"""The Publish page's board: every platform post in a column by where it is in the workflow.

The eleven ``PlatformPost`` statuses are grouped into six columns. Dropping a
card on a column runs the same service the rest of the app uses for that move
(submitting for review, approving, sending back to draft, scheduling), so the
board never changes a status by itself and every rule, record and
notification of those paths applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.composer.models import PlatformPost

S = PlatformPost.Status

# Without a month, published posts older than this stay out of the board; it is a workflow view.
PUBLISHED_WINDOW_DAYS = 30
# Most cards a column renders, so a busy workspace doesn't load its whole history.
COLUMN_LIMIT = 60


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    statuses: tuple[str, ...]
    accepts_drop: bool
    tone: str


COLUMNS: tuple[Column, ...] = (
    Column("draft", gettext_lazy("Draft"), (S.DRAFT,), True, "stone"),
    Column(
        "review", gettext_lazy("In approval"), (S.PENDING_REVIEW, S.PENDING_CLIENT, S.CHANGES_REQUESTED), True, "amber"
    ),
    Column("approved", gettext_lazy("Approved"), (S.APPROVED, S.ON_HOLD), True, "teal"),
    Column("scheduled", gettext_lazy("Scheduled"), (S.SCHEDULED, S.PUBLISHING), True, "blue"),
    Column("published", gettext_lazy("Published"), (S.PUBLISHED,), False, "green"),
    Column("problem", gettext_lazy("Failed or rejected"), (S.FAILED, S.REJECTED), False, "red"),
)
_BY_KEY = {c.key: c for c in COLUMNS}

# Statuses a card may come from for each drop target. Anything else is refused
# with a reason, before any service is called.
DROP_SOURCES = {
    "draft": {S.CHANGES_REQUESTED, S.REJECTED, S.APPROVED, S.ON_HOLD, S.SCHEDULED, S.FAILED},
    "review": {S.DRAFT, S.CHANGES_REQUESTED, S.REJECTED},
    "approved": {S.PENDING_REVIEW},
    "scheduled": {S.DRAFT, S.APPROVED, S.FAILED},
}


class MoveRefusedError(Exception):
    """The drop can't happen; the message says why, for the user."""


def column(key) -> Column | None:
    return _BY_KEY.get(key)


def board_columns(workspace, *, start=None, end=None, platforms=(), tag=""):
    """Columns with their cards, each column's rows ordered by when they go out.

    ``start``/``end`` keep the cards dated in that range (published, scheduled or
    proposed) plus the undated ones; ``platforms`` and ``tag`` narrow them further.
    """
    rows = (
        PlatformPost.objects.filter(post__workspace=workspace)
        .select_related("post", "post__author", "social_account")
        .prefetch_related("post__media_attachments__media_asset")
        .annotate(
            effective_at=Coalesce("scheduled_at", "post__scheduled_at"),
            period_at=Coalesce("published_at", "scheduled_at", "post__scheduled_at", "post__proposed_publish_at"),
        )
        .order_by(F("effective_at").asc(nulls_last=True), "-post__updated_at")
    )
    if start and end:
        rows = rows.filter(Q(period_at__gte=start, period_at__lt=end) | Q(period_at__isnull=True))
    else:
        rows = rows.exclude(status=S.PUBLISHED, published_at__lt=timezone.now() - timedelta(days=PUBLISHED_WINDOW_DAYS))
    if platforms:
        rows = rows.filter(social_account__platform__in=platforms)
    if tag:
        rows = rows.filter(post__tags__contains=[tag])
    by_status: dict[str, list] = {}
    for pp in rows:
        by_status.setdefault(pp.status, []).append(pp)
    result = []
    for col in COLUMNS:
        cards = [pp for status in col.statuses for pp in by_status.get(status, [])]
        if col.key == "published":
            cards.sort(key=lambda pp: pp.published_at or pp.updated_at, reverse=True)
        result.append({"column": col, "cards": cards[:COLUMN_LIMIT], "total": len(cards)})
    return result


def check_move(pp, target: Column, perms: dict, user) -> None:
    """Raise :class:`MoveRefusedError` unless ``user`` may drop ``pp`` on ``target``."""
    if not target.accepts_drop:
        raise MoveRefusedError(_("Posts can't be moved to this column."))
    if pp.status in target.statuses:
        return
    if pp.status not in DROP_SOURCES[target.key]:
        raise MoveRefusedError(
            _("A post that is “%(status)s” can't go to “%(column)s”.")
            % {"status": pp.get_status_display(), "column": target.label}
        )
    if pp.post.author_id != user.pk and not perms.get("edit_others_posts"):
        raise MoveRefusedError(_("You can only move your own posts."))
    if target.key == "approved" and not perms.get("approve_posts"):
        raise MoveRefusedError(_("You don't have permission to approve posts."))
    if target.key == "scheduled":
        if pp.status != S.APPROVED and not perms.get("publish_directly"):
            raise MoveRefusedError(_("This post has to be approved before it is scheduled."))
        when = pp.scheduled_at or pp.post.scheduled_at
        if not when or when <= timezone.now():
            raise MoveRefusedError(_("Open the post and choose when it goes out before scheduling it."))
