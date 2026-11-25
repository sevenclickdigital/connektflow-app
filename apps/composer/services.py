"""Composer service helpers.

Where reusable "build me a post" logic lives so it can be called from the
HTMX-driven composer view *and* from the Agent API without duplicating
state-machine semantics.

The Phase 1 helper ``sync_post_scheduled_at`` keeps ``Post.scheduled_at``
in sync with the earliest per-platform ``PlatformPost.scheduled_at``;
the Phase 2 additions wrap ``Post`` + ``PlatformPost`` creation and
status transitions so audit + idempotency live in one place.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from collections.abc import Iterable
from typing import Any

from django.utils.translation import gettext as _

from apps.tasks import creative_formats


def sync_post_scheduled_at(post):
    """Keep ``Post.scheduled_at`` in sync with the earliest child time.

    Three cases:

    * Some children have a ``scheduled_at`` → set parent to the min.
    * No children have a ``scheduled_at`` AND parent has one cached →
      clear the parent so listings, calendars, and Coalesce fallbacks
      don't keep showing the post as scheduled. This branch covers the
      "agent cancels the last scheduled child" flow — without it,
      ``Post.scheduled_at`` lingered at the old value and the post
      kept appearing as scheduled in the UI.
    * No children, parent already null → no-op.

    A post that gains a committed schedule also drops any draft-stage
    ``proposed_publish_at``: this is the chokepoint the scheduling paths flow
    through (``create_post``, ``transition_platform_post`` for the REST
    ``/schedule`` route and the MCP ``schedule_draft`` tool, and the PATCH
    re-time which calls this after saving), so the suggestion and a real
    schedule never coexist. The ``scheduled_at`` aggregate logic is unchanged —
    the publisher's ``Coalesce(scheduled_at, post__scheduled_at)`` fallback
    depends on it — so only the proposal clear is added here. The one path that
    commits a child WITHOUT a ``scheduled_at`` (the composer's per-account chip
    endpoint) clears the proposal itself rather than routing through here, to
    avoid disturbing that aggregate.
    """
    times = list(post.platform_posts.exclude(scheduled_at__isnull=True).values_list("scheduled_at", flat=True))
    if not times:
        if post.scheduled_at is not None:
            post.scheduled_at = None
            post.save(update_fields=["scheduled_at", "updated_at"])
        return
    earliest = min(times)
    update_fields = []
    if post.scheduled_at != earliest:
        post.scheduled_at = earliest
        update_fields.append("scheduled_at")
    if post.proposed_publish_at is not None:
        post.proposed_publish_at = None
        update_fields.append("proposed_publish_at")
    if update_fields:
        post.save(update_fields=[*update_fields, "updated_at"])


# ---------------------------------------------------------------------------
# Phase 2 — Agent-API-facing post creation
# ---------------------------------------------------------------------------


_INITIAL_STATUSES = {"draft", "scheduled"}


def create_post(
    *,
    workspace,
    social_account,
    caption: str,
    media_asset_ids: Iterable[Any] | None = None,
    title: str = "",
    first_comment: str = "",
    internal_notes: str = "",
    scheduled_at: dt.datetime | None = None,
    proposed_publish_at: dt.datetime | None = None,
    author=None,
    status: str = "draft",
    platform_overrides: dict[Any, dict[str, str | None]] | None = None,
):
    """Create a ``Post`` + one ``PlatformPost`` for ``social_account``.

    The canonical service-layer entry point for the Agent API. The
    composer's HTMX-driven ``save_post`` view will be refactored to call
    this in a follow-up; for now both code paths coexist and only the
    API uses it.

    Contract:

    * ``status`` must be one of ``draft`` or ``scheduled``. Other
      states (``approved``, ``publishing``, …) are produced only via
      explicit transitions through ``transition_platform_post``.
    * If ``status='scheduled'``, ``scheduled_at`` is required and must
      be in the future. The publisher polls every ~15 s; a past
      ``scheduled_at`` is accepted and will fire on the very next tick,
      but the caller almost certainly meant "now-ish" so we leave the
      decision to them.
    * ``proposed_publish_at`` is an optional draft-stage suggestion. It is
      stored as-is regardless of ``status`` and carries no future-time
      requirement — it never reaches the publisher.
    * ``social_account.workspace_id`` must equal ``workspace.id``. The
      caller (auth class / view) already enforced scope, but we
      double-check here so anyone reaching the service directly can't
      bypass it.
    * Media assets are attached in order by their position in
      ``media_asset_ids``. They must already live in the workspace's
      media library.

    Returns the persisted ``Post`` with one ``PlatformPost`` child.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPost, Post, PostMedia
    from apps.media_library.models import MediaAsset

    if status not in _INITIAL_STATUSES:
        raise ValueError(
            f"create_post can only initialise status in {sorted(_INITIAL_STATUSES)}; "
            f"got {status!r}. Use transition_platform_post for other states."
        )
    if status == "scheduled" and scheduled_at is None:
        raise ValueError("status='scheduled' requires scheduled_at.")
    if social_account.workspace_id != workspace.id:
        raise ValueError(f"SocialAccount {social_account.id} is not in workspace {workspace.id}.")

    # Refuse to queue work against a connection the platform itself has
    # rejected. ``failed`` rows still count against the platform quota
    # (see ``QUOTA_CONSUMING_STATUSES`` in apps/api/limits.py), so each
    # silently-scheduled post against a disconnected/errored account
    # would burn one slot without ever publishing. Forcing an explicit
    # error here makes the agent reconnect first.
    if getattr(social_account, "needs_reconnect", False):
        raise ValueError(
            f"SocialAccount {social_account.id} is in connection_status "
            f"{social_account.connection_status!r}; reconnect it before scheduling."
        )

    if status == "scheduled":
        _require_approval_gate_passes(workspace)

    media_ids = list(media_asset_ids or [])
    # Pull and validate all referenced assets in a single query — fail
    # closed if any ID is missing or belongs to a different workspace.
    #
    # Normalize every id to a ``UUID`` for keying/lookup so all caller shapes
    # match identically: UUID objects (REST, coerced by Pydantic) and UUID
    # *strings* in any form (MCP, off the JSON-RPC wire — including uppercase or
    # braced). A raw string never compares equal to a UUID key, so without this
    # MCP-supplied assets are wrongly rejected as "not found in workspace".
    asset_map: dict[uuid.UUID, MediaAsset] = {}
    resolved: list[tuple[Any, uuid.UUID | None]] = []
    if media_ids:

        def _to_uuid(value):
            try:
                return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
            except (ValueError, TypeError, AttributeError):
                return None

        resolved = [(mid, _to_uuid(mid)) for mid in media_ids]
        found = list(
            MediaAsset.objects.filter(id__in=[u for _key, u in resolved if u is not None], workspace=workspace)
        )
        asset_map = {a.id: a for a in found}
        missing = [mid for mid, u in resolved if u is None or u not in asset_map]
        if missing:
            raise ValueError(f"Media asset(s) not found in workspace {workspace.id}: {missing}")

    if status == "scheduled":
        # A new post has no platform_extra, so a Pinterest pin has no board
        # yet and is refused here: it can be created as a draft, given a
        # board in the composer, and scheduled from there.
        reason = publish_blocker(
            social_account,
            media_types=usable_media_types(asset_map.values()),
            image_count=sum(1 for asset in asset_map.values() if asset.file and asset.media_type == "image"),
        )
        if reason:
            raise ValueError(reason)

    override = (platform_overrides or {}).get(social_account.id) or {}

    with transaction.atomic():
        post = Post.objects.create(
            workspace=workspace,
            author=author,
            title=title,
            caption=caption,
            first_comment=first_comment,
            internal_notes=internal_notes,
            scheduled_at=scheduled_at if status == "scheduled" else None,
            # A proposal is plain draft metadata — keep it independent of
            # ``status`` (unlike ``scheduled_at``, which only exists once the
            # post is committed to the publisher).
            proposed_publish_at=proposed_publish_at,
        )
        # Each override field is independent: ``None`` (or omitted)
        # means "no platform-specific override; fall back to the post
        # value via ``PlatformPost.effective_*``". Any string —
        # including ``""`` — is treated as an explicit override.
        PlatformPost.objects.create(
            post=post,
            social_account=social_account,
            status=status,
            scheduled_at=scheduled_at if status == "scheduled" else None,
            platform_specific_title=override.get("title"),
            platform_specific_caption=override.get("caption"),
            platform_specific_first_comment=override.get("first_comment"),
        )
        for position, (_mid, u) in enumerate(resolved):
            # ``u`` is validated non-None and present in ``asset_map`` above
            # (the ``missing`` check raises otherwise); narrow for mypy.
            assert u is not None
            PostMedia.objects.create(
                post=post,
                media_asset=asset_map[u],
                position=position,
            )

    # Keep the aggregate column consistent — the publisher polls
    # PlatformPost directly so this is just for listings/UI, but the
    # composer view code relies on it.
    sync_post_scheduled_at(post)
    return post


def transition_platform_post(
    platform_post,
    target_status: str,
    *,
    scheduled_at: dt.datetime | None = None,
):
    """Apply a state-machine transition and persist.

    Wraps ``PlatformPost.transition_to`` (which only mutates the model
    in memory) with the persistence + side-effects the API needs:

    * Save the new status.
    * On ``scheduled``, set ``scheduled_at`` if provided, and sync the
      aggregate ``Post.scheduled_at`` column.
    * On ``draft``, clear ``scheduled_at`` so the row stops being a
      target for the publisher's polling query.

    Raises ``ValueError`` (from ``transition_to``) if the transition
    isn't in ``VALID_TRANSITIONS``.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPost

    # Approval workflow gate — Codex review (PR #53) flagged that the
    # ``POST /posts/{id}/schedule`` route reached this function with
    # ``target_status="scheduled"`` and bypassed the approval check
    # ``create_post`` enforces. The gate belongs here so EVERY path that
    # promotes a draft to scheduled (REST `/schedule` route, any future
    # MCP transition tool, the composer's HTMX `transition_platform_post`
    # view) is covered.
    if target_status == "scheduled":
        _require_approval_gate_passes(platform_post.post.workspace)
        require_publishable(platform_post)

    with transaction.atomic():
        platform_post.transition_to(target_status)
        # Always include ``updated_at`` — Django docs: when ``update_fields``
        # is explicit, ``auto_now`` fields are NOT touched unless listed.
        # Without this every API/MCP-driven transition would leave
        # ``PlatformPost.updated_at`` frozen at creation time, breaking
        # any "modified since X" sync and ORDER BY updated_at view.
        # TRANSITION_FIELDS covers everything transition_to writes (the retry
        # budget and the publish handle, not just status), so the REST/MCP
        # scheduling path persists the same reset the composer does.
        update_fields = {*PlatformPost.TRANSITION_FIELDS, "updated_at"}
        if target_status == "scheduled":
            if scheduled_at is not None:
                platform_post.scheduled_at = scheduled_at
                update_fields.add("scheduled_at")
        elif target_status == "draft":
            if platform_post.scheduled_at is not None:
                platform_post.scheduled_at = None
                update_fields.add("scheduled_at")
        elif target_status == "published":
            update_fields.add("published_at")  # set by transition_to
        platform_post.save(update_fields=list(update_fields))
        if target_status == "draft":
            # A drafted child is no longer queued — drop its QueueEntry so the
            # slot frees as a gap. Covers cancel / unschedule paths that would
            # otherwise orphan the row with a stale assigned_slot_datetime.
            from apps.calendar.services import drop_queue_entry_unless_scheduled

            drop_queue_entry_unless_scheduled(platform_post.post_id, platform_post.social_account_id)

    sync_post_scheduled_at(platform_post.post)
    return platform_post


def can_edit_post(post, user, effective_permissions) -> bool:
    """Whether ``user`` may edit, unschedule or schedule ``post``.

    Authors change their own posts; anyone else needs ``edit_others_posts``.
    A post with no author (its author was deleted) belongs to nobody.
    """
    if effective_permissions.get("edit_others_posts", False):
        return True
    user_id = getattr(user, "id", None)
    return user_id is not None and post.author_id == user_id


#: Hosts a link to a post on each network may point at. A link is read from
#: the network's API and opened from our pages, so it is only followed there.
PERMALINK_HOSTS = {
    "facebook": ("facebook.com", "fb.watch"),
    "instagram": ("instagram.com",),
    "instagram_login": ("instagram.com",),
    "threads": ("threads.net", "threads.com"),
}

#: Networks whose post link cannot be built from the post id: it is asked for
#: when someone opens the post (``fetch_post_permalink``).
PERMALINK_LOOKUP_PLATFORMS = ("instagram", "instagram_login", "threads")


def _trusted_permalink(platform: str, url: object) -> str:
    """``url`` when it is an https link on one of the network's own hosts, else ""."""
    from urllib.parse import urlsplit

    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(
        host == h or host.endswith(f".{h}") for h in PERMALINK_HOSTS.get(platform, ())
    ):
        return ""
    return parts.geturl()


def post_permalink(platform_post) -> str:
    """A link to the published post on its network that needs no call to it, or "".

    A post imported from the profile carries the link the network listed it
    with. For the rest it is built from ``platform_post_id`` where the network
    addresses a post by its id: Facebook resolves ``/<id>`` on its own (the id
    is stored bare, without the Page's), YouTube by the video id. Instagram and
    Threads have no id-addressable link (``PERMALINK_LOOKUP_PLATFORMS``).
    """
    from urllib.parse import quote

    post_id = (getattr(platform_post, "platform_post_id", "") or "").strip()
    if not post_id:
        return ""
    platform = getattr(platform_post.social_account, "platform", "")
    imported = _trusted_permalink(platform, (platform_post.platform_extra or {}).get("permalink"))
    if imported:
        return imported
    if platform == "facebook":
        return f"https://www.facebook.com/{quote(post_id)}"
    if platform == "youtube":
        return f"https://www.youtube.com/watch?v={quote(post_id)}"
    return ""


def post_view_url(platform_post) -> str:
    """Where "View on <network>" leads for a published row, or "" when nothing can open it.

    The post's own link when it is known without asking the network, else the
    page that asks for it and redirects (``composer:post_open``).
    """
    from django.urls import reverse

    if platform_post.status != platform_post.Status.PUBLISHED or not platform_post.platform_post_id:
        return ""
    link = post_permalink(platform_post)
    if link:
        return link
    if platform_post.social_account.platform in PERMALINK_LOOKUP_PLATFORMS:
        return reverse(
            "composer:post_open",
            kwargs={"workspace_id": platform_post.post.workspace_id, "platform_post_id": platform_post.id},
        )
    return ""


def fetch_post_permalink(platform_post) -> str:
    """The post's link, asking the network for it when it cannot be built. Raises ``ProviderError``.

    "" when the network answers without a link it can be trusted to be its own.
    """
    link = post_permalink(platform_post)
    if link or not platform_post.platform_post_id:
        return link
    from apps.publisher.engine import permalink_on_network

    return _trusted_permalink(platform_post.social_account.platform, permalink_on_network(platform_post))


class PostEditError(Exception):
    """A published post's text could not be changed; the message is safe to show."""


def edit_published_text(platform_post, text: str) -> None:
    """Change the text of a published row's post on its network, then here.

    ``text`` replaces the row's own caption; the post's published tags stay
    after it, as they went out. Nothing is kept here when the network refuses.
    Raises ``PostEditError`` with the reason.
    """
    from apps.publisher.engine import edit_on_network
    from apps.social_accounts.error_messages import friendly_edit_error
    from providers.exceptions import ProviderError

    if not platform_post.network_edit:
        raise PostEditError(_("This post's text can't be changed on the network from here."))
    text = (text or "").strip()
    if not text:
        raise PostEditError(_("Write the post's text."))
    previous = platform_post.platform_specific_caption
    platform_post.platform_specific_caption = text
    if platform_post.is_over_limit:
        platform_post.platform_specific_caption = previous
        raise PostEditError(
            _("The text is longer than the %(limit)s characters this network accepts.")
            % {"limit": platform_post.char_limit}
        )
    try:
        edit_on_network(platform_post, platform_post.effective_caption)
    except ProviderError as exc:
        platform_post.platform_specific_caption = previous
        raise PostEditError(friendly_edit_error(exc)) from exc
    platform_post.save(update_fields=["platform_specific_caption", "updated_at"])


def base_content_snapshot(post):
    """Reviewable base content used to detect edits to an approved post."""
    return (post.title, post.caption, post.first_comment, tuple(post.tags or []))


#: Rows whose content a reviewer has cleared: approved, or approved and then scheduled.
_REVIEWED_STATUSES = ("approved", "scheduled")


def reviewed_content_snapshot(post):
    """Everything of ``post`` that is published and that a reviewer cleared.

    The base fields and the ordered media, plus — for each row already
    approved or scheduled — what that row publishes instead of the base: its
    own title, caption and first comment, and its platform options. Compare two
    of them with :func:`reviewed_content_changed`.
    """
    rows = {
        pp.id: (
            pp.platform_specific_title,
            pp.platform_specific_caption,
            pp.platform_specific_first_comment,
            pp.platform_extra or {},
        )
        for pp in post.platform_posts.filter(status__in=_REVIEWED_STATUSES)
    }
    media = tuple(post.media_attachments.order_by("position", "id").values_list("media_asset_id", flat=True))
    return (*base_content_snapshot(post), media), rows


def reviewed_content_changed(before, after) -> bool:
    """Whether a write changed what the reviewer cleared (see ``reviewed_content_snapshot``).

    A row that was added or removed in between is not a change to the others.
    """
    shared_before, rows_before = before
    shared_after, rows_after = after
    if shared_before != shared_after:
        return True
    return any(rows_after.get(row_id, content) != content for row_id, content in rows_before.items())


def revert_approved_to_review(post, *, include_scheduled=False):
    """Option A: editing an approved post's content sends it back for re-approval.

    Silently reverts any ``approved`` children to ``pending_review`` so edited
    content can't publish without a fresh review. (The explicit "Resubmit for
    review" button additionally notifies reviewers; this is the safety net for
    plain saves/autosaves and the Agent API's PATCH.) Returns the reverted children.

    ``include_scheduled`` also takes ``scheduled`` children out of the publish
    path and back to review. Pass it when the member who edited needs an
    approval to schedule (``needs_approval_to_schedule``): their scheduled rows
    got there on a reviewer's word for the content as it was.
    """
    from apps.composer.models import PlatformPost

    reverted = []
    for pp in post.platform_posts.all():
        if include_scheduled and pp.status == "scheduled":
            # There is no scheduled → pending_review edge; draft is the way out.
            pp.transition_to("draft")
        elif pp.status != "approved":
            continue
        if pp.can_transition_to("pending_review"):
            pp.transition_to("pending_review")
            pp.save(update_fields=[*PlatformPost.TRANSITION_FIELDS, "updated_at"])
            reverted.append(pp)
    return reverted


def create_draft_post(*, workspace, author, caption: str, title: str = "", media_asset_ids: Iterable[Any] = ()):
    """A draft ``Post`` aimed at every connected account, for the composer to finish.

    Unlike :func:`create_post` (one account, possibly scheduled), this is the
    "open it in the composer" shape: the user picks accounts, crops and time
    there. Media must already be in the workspace's library and is attached
    in the given order.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPost, Post, PostMedia
    from apps.social_accounts.models import SocialAccount

    accounts = (
        SocialAccount.objects.for_workspace(workspace.id)
        .publishable()
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
    )
    with transaction.atomic():
        post = Post.objects.create(workspace=workspace, author=author, title=title[:255], caption=caption)
        PostMedia.objects.bulk_create(
            [PostMedia(post=post, media_asset_id=asset_id, position=i) for i, asset_id in enumerate(media_asset_ids)]
        )
        PlatformPost.objects.bulk_create([PlatformPost(post=post, social_account=account) for account in accounts])
    return post


def clone_post(post, *, author=None):
    """Create a fresh DRAFT duplicate of *post* (Clone / Repost).

    Copies caption, title, first comment, internal notes, tags, category, every
    per-platform override (``platform_specific_*`` + ``platform_extra``) and all
    media attachments. The copy starts as a ``draft`` with no schedule, so a
    published (or any) post can be re-edited and re-queued without touching the
    original. ``author`` defaults to the source author.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPost, Post, PostMedia
    from apps.social_accounts.youtube_compliance import authored_youtube_settings

    with transaction.atomic():
        new_post = Post.objects.create(
            workspace=post.workspace,
            author=author or post.author,
            title=(f"Copy of {post.title}" if post.title else ""),
            caption=post.caption,
            first_comment=post.first_comment,
            internal_notes=post.internal_notes,
            tags=post.tags,
            category=post.category,
        )
        children = list(post.platform_posts.all())
        if children:
            PlatformPost.objects.bulk_create(
                [
                    PlatformPost(
                        post=new_post,
                        social_account=pp.social_account,
                        format=pp.format,
                        status="draft",
                        platform_specific_title=pp.platform_specific_title,
                        platform_specific_caption=pp.platform_specific_caption,
                        platform_specific_first_comment=pp.platform_specific_first_comment,
                        platform_specific_media=copy.deepcopy(pp.platform_specific_media),
                        platform_extra=copy.deepcopy(
                            authored_youtube_settings(pp.platform_extra)
                            if pp.social_account.platform == "youtube"
                            else pp.platform_extra or {}
                        ),
                    )
                    for pp in children
                ]
            )
        media = list(post.media_attachments.all())
        if media:
            PostMedia.objects.bulk_create(
                [
                    PostMedia(
                        post=new_post,
                        media_asset=pm.media_asset,
                        position=pm.position,
                        alt_text=pm.alt_text,
                        platform_overrides=copy.deepcopy(pm.platform_overrides),
                    )
                    for pm in media
                ]
            )
    return new_post


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Workspace approval modes that BLOCK a direct draft → scheduled
#: transition. The composer's HTMX path routes around this via a
#: separate ``submit_for_approval`` action; the Agent API surface uses
#: the same service layer so the constraint lives here once.
_APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE = frozenset({"required_internal", "required_internal_and_client"})


def _require_approval_gate_passes(workspace) -> None:
    """Raise ``ValueError`` if the workspace forbids direct scheduling.

    Drafts are always allowed (they're explicit save-for-later);
    scheduling implies "ready to publish," which is exactly what the
    approval workflow gates.
    """
    if getattr(workspace, "approval_workflow_mode", "none") in _APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE:
        raise ValueError(
            "Workspace requires approval before scheduling; create the post as a "
            "draft and route it through the approval workflow."
        )


def needs_approval_to_schedule(workspace, effective_permissions) -> bool:
    """Whether a member with these permissions may only schedule content a reviewer approved.

    True in a workspace whose approval workflow is required, for a member
    without ``publish_directly``.
    """
    return workspace.approval_workflow_mode in _APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE and not (
        effective_permissions.get("publish_directly", False)
    )


def usable_media_types(assets) -> set[str]:
    """The media types among ``assets`` the publisher will actually upload.

    It skips an attachment whose asset has no stored file, so one of those
    can't satisfy a platform that needs media — or a video.
    """
    return {asset.media_type for asset in assets if asset.file}


def _usable_attachments():
    """``PostMedia`` rows ``usable_media_types`` would count, as a queryset."""
    from apps.composer.models import PostMedia

    return PostMedia.objects.exclude(media_asset__file="")


def post_media_types(post, *, excluding=None) -> set[str]:
    """``usable_media_types`` for ``post``'s attachments, in one query.

    ``excluding`` (a ``PostMedia``) answers for the post as it would be once
    that attachment is removed.
    """
    attachments = _usable_attachments().filter(post_id=post.pk)
    if excluding is not None:
        attachments = attachments.exclude(pk=excluding.pk)
    return set(attachments.values_list("media_asset__media_type", flat=True).distinct())


def media_types_by_post(post_ids) -> dict:
    """``post_media_types`` for many posts in one query; absent ids have none."""
    by_post: dict = {}
    rows = _usable_attachments().filter(post_id__in=post_ids).values_list("post_id", "media_asset__media_type")
    for post_id, media_type in rows.distinct():
        by_post.setdefault(post_id, set()).add(media_type)
    return by_post


def media_blocker(social_account, *, media_types) -> str:
    """Why ``social_account`` can't publish a post with these media, or "".

    TikTok and YouTube publish only video; Instagram and Pinterest need an
    image or a video. Their providers refuse anything else before calling out.
    """
    from apps.media_library.models import MediaAsset
    from providers import is_video_only, requires_media

    platform = social_account.get_platform_display()
    if is_video_only(social_account.platform) and MediaAsset.MediaType.VIDEO not in media_types:
        return _("%(platform)s can only publish videos. Add a video to this post before scheduling it.") % {
            "platform": platform
        }
    if requires_media(social_account.platform) and not media_types:
        return _("%(platform)s needs an image or a video. Add one to this post before scheduling it.") % {
            "platform": platform
        }
    return ""


def format_blocker(format: str, ordered_media_types) -> str:
    """Why media in this order can't be published as ``format``, or "".

    ``ordered_media_types`` is the type of each usable attachment by position;
    only the first two matter. The default format accepts anything here — what
    a platform needs from it is ``media_blocker``'s business.
    """
    from providers.types import FORMAT_POST_TYPES, media_fits_post_type

    post_type = FORMAT_POST_TYPES.get(format)
    if post_type is None:
        return ""
    types = list(ordered_media_types)
    if media_fits_post_type(post_type, len(types), types[0] if types else None):
        return ""
    if format == "story":
        return _("A Story needs at least one photo or video.")
    if format == "reel":
        return _("A Reel needs exactly one video.")
    if format == "short":
        return _("A Short needs exactly one video.")
    return _("A profile photo needs exactly one image.")


def post_ordered_media_types(post, *, excluding=None) -> list[str]:
    """The type of each usable attachment of ``post``, by position (first two only)."""
    attachments = _usable_attachments().filter(post_id=post.pk)
    if excluding is not None:
        attachments = attachments.exclude(pk=excluding.pk)
    return list(attachments.order_by("position").values_list("media_asset__media_type", flat=True)[:2])


def format_media_replacements(platform_posts):
    """``{platform_post_id: {source_asset_id: MediaAsset}}`` for rows with their own crops.

    A format can publish its own crop of an attachment (a 9:16 copy for the
    Story while the feed keeps the original). The row lists those copies in
    ``platform_specific_media``; each names, through ``derived_from``, the
    attachment it replaces. A copy whose source is no longer attached simply
    matches nothing. One query for all the rows given.
    """
    from apps.media_library.models import MediaAsset

    wanted = {pp.pk: [str(i) for i in (pp.platform_specific_media or [])] for pp in platform_posts}
    all_ids = {i for ids in wanted.values() for i in ids}
    if not all_ids:
        return {}
    assets = {str(a.pk): a for a in MediaAsset.objects.filter(pk__in=all_ids, derived_from__isnull=False)}
    return {
        pk: {assets[i].derived_from_id: assets[i] for i in ids if i in assets}
        for pk, ids in wanted.items()
        if any(i in assets for i in ids)
    }


def apply_format_media(platform_post, attachments):
    """``attachments`` (PostMedia, in order) with this row's own crops swapped in.

    Swaps the ``media_asset`` on the in-memory objects only; nothing is saved.
    """
    replacements = format_media_replacements([platform_post]).get(platform_post.pk, {})
    for pm in attachments:
        if pm.media_asset_id in replacements:
            pm.media_asset = replacements[pm.media_asset_id]
    return attachments


def set_format_crop(platform_posts, cropped):
    """Make ``cropped`` the crop these rows publish in place of its source.

    Replaces any earlier crop of the same source; crops of other attachments
    are kept.
    """
    from apps.media_library.models import MediaAsset

    for pp in platform_posts:
        current = [str(i) for i in (pp.platform_specific_media or [])]
        same_source = {
            str(i)
            for i in MediaAsset.objects.filter(pk__in=current, derived_from_id=cropped.derived_from_id).values_list(
                "pk", flat=True
            )
        }
        pp.platform_specific_media = [i for i in current if i not in same_source] + [str(cropped.pk)]
        pp.save(update_fields=["platform_specific_media", "updated_at"])


def post_image_count(post) -> int:
    """How many usable images ``post`` has attached."""
    return _usable_attachments().filter(post_id=post.pk, media_asset__media_type="image").count()


def image_count_blocker(social_account, *, image_count: int, format: str = "default") -> str:
    """Why this many images can't go out as one feed post of ``social_account``, or "".

    Only the feed post has the limit (``SocialProvider.max_images_per_post``):
    a Story with several photos publishes them one by one.
    """
    from providers import max_images_per_post

    limit = max_images_per_post(social_account.platform)
    if format != "default" or limit is None or image_count <= limit:
        return ""
    return _(
        "%(platform)s takes up to %(limit)d photos in one post and this one has %(count)d. "
        "Remove some before scheduling it."
    ) % {"platform": social_account.get_platform_display(), "limit": limit, "count": image_count}


def publish_blocker(
    social_account,
    *,
    media_types,
    platform_extra: dict | None = None,
    format: str = "default",
    ordered_media_types=None,
    image_count: int | None = None,
) -> str:
    """Why a post can't publish to ``social_account`` as it stands, or "".

    The one statement of this rule. The publisher checks it before calling a
    platform, and every path that schedules a post checks it first so the user
    hears about it then rather than after the post's other platforms have gone
    out. Only failures that are certain before the platform is ever called: the
    media a platform can't do without (``media_blocker``), more photos than a
    feed post carries (``image_count_blocker``, when the caller knows the
    count), and a pin with no board, which our own Pinterest provider refuses.
    """
    reason = media_blocker(social_account, media_types=media_types)
    if reason:
        return reason
    if image_count is not None:
        reason = image_count_blocker(social_account, image_count=image_count, format=format)
        if reason:
            return reason
    if ordered_media_types is not None:
        reason = format_blocker(format, ordered_media_types)
        if reason:
            return reason
    if social_account.platform == "pinterest" and not (platform_extra or {}).get("board_id"):
        return _(
            "Pinterest needs a board for every pin. Choose one for this post in the composer before scheduling it."
        )
    return ""


def require_publishable(platform_post, *, media_types=None) -> None:
    """Raise ``ValueError`` if ``platform_post`` is certain to fail at publish time.

    ``media_types`` lets a caller checking many rows answer it for all of them
    in one query (``media_types_by_post``) instead of one per row.
    """
    from providers import max_images_per_post

    if media_types is None:
        media_types = post_media_types(platform_post.post)
    limited = max_images_per_post(platform_post.social_account.platform) is not None
    reason = publish_blocker(
        platform_post.social_account,
        media_types=media_types,
        platform_extra=platform_post.platform_extra,
        format=platform_post.format,
        image_count=post_image_count(platform_post.post) if limited else None,
        ordered_media_types=(
            post_ordered_media_types(platform_post.post) if platform_post.format != "default" else None
        ),
    )
    if reason:
        raise ValueError(reason)


def media_change_blocker(post, *, media_types_after, ordered_media_types_after=None) -> str:
    """Why a media change would strand a scheduled row, or "".

    Scheduling checks the post as it stood then; this stops the media a
    platform needs being taken away afterwards — the video from a TikTok post,
    the last image from a pin — which would leave the row queued to fail. Only
    rows already ``scheduled`` count; anything else is checked again when it is
    scheduled.
    """
    from apps.composer.models import PlatformPost
    from providers import is_video_only

    children = post.platform_posts.filter(status=PlatformPost.Status.SCHEDULED).select_related("social_account")
    for child in children:
        if child.format != PlatformPost.Format.DEFAULT and ordered_media_types_after is not None:
            reason = format_blocker(child.format, ordered_media_types_after)
            if reason:
                return _("This post is scheduled as %(format)s on %(platform)s. %(reason)s") % {
                    "format": child.get_format_display(),
                    "platform": child.social_account.get_platform_display(),
                    "reason": reason,
                }
        if media_blocker(child.social_account, media_types=media_types_after):
            platform = child.social_account.get_platform_display()
            # Two whole sentences rather than one with a slot for the media
            # kind: a translation may need to reshape the clause around it.
            if is_video_only(child.social_account.platform):
                template = _(
                    "This post is scheduled to %(platform)s, which needs a video on it. "
                    "Keep one on the post, or unschedule it for %(platform)s first."
                )
            else:
                template = _(
                    "This post is scheduled to %(platform)s, which needs an image or a video on it. "
                    "Keep one on the post, or unschedule it for %(platform)s first."
                )
            return template % {"platform": platform}
    return ""


def brand_hashtags(workspace_id, *, limit: int = 12, sample: int = 200) -> list[dict]:
    """The hashtags the brand publishes with, most used first.

    Read from the captions and first comments of its latest ``sample``
    published platform posts, as they went out (tags published with the post
    included). ``[{"tag", "posts", "reach"}]``: ``posts`` is how many
    different posts carried the tag; ``reach`` is the average reach of the
    publications that have that number, or ``None`` where no platform
    reported it. Nothing is asked of any platform here.
    """
    import re
    from collections import defaultdict

    from apps.analytics.models import PostInsightsSnapshot

    from .models import PlatformPost

    platform_posts = list(
        PlatformPost.objects.filter(post__workspace_id=workspace_id, status="published")
        .select_related("post")
        .order_by("-published_at")[:sample]
    )
    if not platform_posts:
        return []

    # The latest reach each publication reported (the value is cumulative).
    reach = {}
    for platform_post_id, value in (
        PostInsightsSnapshot.objects.filter(platform_post__in=platform_posts, metric_key="reach")
        .order_by("platform_post_id", "date")
        .values_list("platform_post_id", "value")
    ):
        reach[platform_post_id] = value

    posts = defaultdict(set)
    reaches = defaultdict(list)
    for platform_post in platform_posts:
        text = f"{platform_post.effective_caption}\n{platform_post.effective_first_comment or ''}"
        for tag in {match.lower() for match in re.findall(r"#(\w{2,100})", text)}:
            posts[tag].add(platform_post.post_id)
            if platform_post.id in reach:
                reaches[tag].append(reach[platform_post.id])

    ranked = sorted(posts, key=lambda tag: (-len(posts[tag]), tag))[:limit]
    return [
        {
            "tag": tag,
            "posts": len(posts[tag]),
            "reach": round(sum(reaches[tag]) / len(reaches[tag])) if reaches[tag] else None,
        }
        for tag in ranked
    ]


class TagGroupNameTakenError(ValueError):
    """Another group of the brand already has that name."""


def _tags_named(workspace, names):
    """The brand's ``Tag`` rows for ``names``, creating (as internal) the
    ones it does not have yet."""
    from .models import Tag

    names = list(dict.fromkeys(names))
    existing = set(Tag.objects.for_workspace(workspace.id).filter(name__in=names).values_list("name", flat=True))
    missing = [Tag(workspace=workspace, name=name) for name in names if name not in existing]
    if missing:
        Tag.objects.bulk_create(missing, ignore_conflicts=True)
    return Tag.objects.for_workspace(workspace.id).filter(name__in=names)


def tag_group_payload(group) -> dict:
    """``{"id", "name", "tags"}`` of one group, tags by name."""
    return {"id": str(group.id), "name": group.name, "tags": sorted(tag.name for tag in group.tags.all())}


def tag_groups_payload(workspace_id) -> list[dict]:
    """The brand's tag groups for the tag pickers, by name."""
    from .models import TagGroup

    groups = TagGroup.objects.for_workspace(workspace_id).prefetch_related("tags")
    return [tag_group_payload(group) for group in groups]


#: Networks ignore hashtags past about five, so an idea and a tag group carry at most this many
#: tags (the pickers stop there too: ``tagLimit`` in composer/partials/_tag_groups.html).
MAX_IDEA_TAGS = 5
MAX_GROUP_TAGS = 5


def save_tag_group(workspace, name, tag_names, group=None):
    """Create a group, or rename ``group`` and set its tags to ``tag_names`` (the first
    ``MAX_GROUP_TAGS`` of them).

    Raises ``TagGroupNameTakenError`` when another group of the brand has the name.
    """
    tag_names = list(dict.fromkeys(tag_names))[:MAX_GROUP_TAGS]
    from django.db import transaction

    from .models import TagGroup

    taken = TagGroup.objects.for_workspace(workspace.id).filter(name=name)
    if group is not None:
        taken = taken.exclude(id=group.id)
    if taken.exists():
        raise TagGroupNameTakenError(name)
    with transaction.atomic():
        if group is None:
            group = TagGroup.objects.create(workspace=workspace, name=name)
        elif group.name != name:
            group.name = name
            group.save(update_fields=["name"])
        group.tags.set(_tags_named(workspace, tag_names))
    return group


def add_to_tag_group(workspace, name, tag_names) -> None:
    """Put ``tag_names`` in the brand's group called ``name``, creating it
    when needed and keeping what it already holds, up to ``MAX_GROUP_TAGS``.
    Safe to repeat."""
    from .models import TagGroup

    if not name or not tag_names:
        return
    group, _created = TagGroup.objects.get_or_create(workspace=workspace, name=name)
    held = set(group.tags.values_list("name", flat=True))
    room = MAX_GROUP_TAGS - len(held)
    new_names = [n for n in dict.fromkeys(tag_names) if n not in held][: max(room, 0)]
    if new_names:
        group.tags.add(*_tags_named(workspace, new_names))


#: How many of the most used templates lately are shown as popular, and over how many days.
POPULAR_TEMPLATES = 3
POPULAR_TEMPLATE_DAYS = 90


def popular_template_ids() -> set[int]:
    """The templates used most in the last ``POPULAR_TEMPLATE_DAYS`` days,
    across every brand: at most ``POPULAR_TEMPLATES``, and only ones used."""
    from django.db.models import Count
    from django.utils import timezone

    from .models import IdeaTemplateUse

    since = timezone.now() - dt.timedelta(days=POPULAR_TEMPLATE_DAYS)
    ranked = (
        IdeaTemplateUse.objects.filter(created_at__gte=since, template__is_active=True)
        .values("template")
        .annotate(total=Count("id"))
        .order_by("-total", "template")[:POPULAR_TEMPLATES]
    )
    return {row["template"] for row in ranked}


#: The account platforms behind each network a template names.
TEMPLATE_NETWORK_PLATFORMS = {
    "instagram": {"instagram", "instagram_login"},
    "facebook": {"facebook"},
    "linkedin": {"linkedin_personal", "linkedin_company"},
    "tiktok": {"tiktok"},
    "youtube": {"youtube"},
    "threads": {"threads"},
    "pinterest": {"pinterest"},
    "x": {"x"},
}


#: The account platform whose publishing formats a template network offers.
TEMPLATE_NETWORK_FORMAT_SOURCE = {"linkedin": "linkedin_company"}


def template_network_formats(network) -> list[str]:
    """The publishing formats a template may name for ``network``: those the
    composer offers on its accounts (``SocialAccount.PLATFORM_FIELD_CONFIG``)."""
    from apps.social_accounts.models import SocialAccount

    platform = TEMPLATE_NETWORK_FORMAT_SOURCE.get(network, network)
    return SocialAccount.PLATFORM_FIELD_CONFIG.get(platform, {}).get(
        "formats", SocialAccount.PLATFORM_FIELD_DEFAULTS["formats"]
    )


def template_format_short(fmt, formats) -> str:
    """A format's short name, as the composer's toggles call it."""
    from django.utils.translation import pgettext

    from apps.social_accounts.models import default_format_short

    names = {
        "default": default_format_short(formats),
        "story": pgettext("publishing format", "Story"),
        "reel": _("Reels"),
        "short": _("Shorts"),
        "profile_photo": _("Photo"),
    }
    return str(names.get(fmt, fmt))


def networks_in_account_order(accounts=()) -> list[str]:
    """Every template network: those of ``accounts`` first, in the accounts' order
    (the brand's, ``SocialAccountQuerySet.in_display_order``), then the rest."""
    from .models import IdeaTemplate

    first = list(
        dict.fromkeys(network for network in map(network_of_platform, (a.platform for a in accounts)) if network)
    )
    return first + [network for network in IdeaTemplate.Network.values if network not in first]


def template_network_options(accounts=()) -> list[dict]:
    """Every network a template can name, with its formats, for the forms:
    ``[{"network", "label", "formats": [{"value": "<network>:<format>", "short"}]}]``,
    the networks of ``accounts`` first (``networks_in_account_order``)."""
    from .models import IdeaTemplate

    labels = dict(IdeaTemplate.Network.choices)
    options = []
    for network in networks_in_account_order(accounts):
        formats = template_network_formats(network)
        options.append(
            {
                "network": network,
                "label": str(labels[network]),
                "formats": [
                    {"value": f"{network}:{fmt}", "format": fmt, "short": template_format_short(fmt, formats)}
                    for fmt in formats
                ],
            }
        )
    return options


def network_formats_from_targets(targets) -> dict[str, list[str]]:
    """``{"network": [formats]}`` from ``"<network>:<format>"`` values, keeping
    only real networks and the formats each offers, in the network's order."""
    from .models import IdeaTemplate

    picked: dict[str, set[str]] = {}
    for value in targets or []:
        network, _sep, fmt = str(value).partition(":")
        if network in IdeaTemplate.Network.values and fmt in template_network_formats(network):
            picked.setdefault(network, set()).add(fmt)
    return {
        network: [fmt for fmt in template_network_formats(network) if fmt in picked[network]]
        for network in IdeaTemplate.Network.values
        if network in picked
    }


def template_network_items(template, accounts=()) -> list[dict]:
    """The template's networks for its panel, each with the formats picked:
    ``[{"network", "label", "formats": [{"format", "short"}]}]``, the networks of
    ``accounts`` first (``networks_in_account_order``)."""
    from .models import IdeaTemplate

    labels = dict(IdeaTemplate.Network.choices)
    items = []
    for network in networks_in_account_order(accounts):
        picked = (template.network_formats or {}).get(network)
        if picked is None and network not in (template.networks or []):
            continue
        offered = template_network_formats(network)
        formats = [fmt for fmt in offered if fmt in (picked or ["default"])]
        items.append(
            {
                "network": network,
                "label": str(labels[network]),
                "formats": [{"format": fmt, "short": template_format_short(fmt, offered)} for fmt in formats],
            }
        )
    return items


def template_network_labels(template, accounts=()) -> list[str]:
    """The template's networks as its panel lists them: "Instagram (Feed, Reels)",
    or just the name where the network has only the one format; the networks of
    ``accounts`` first (``networks_in_account_order``)."""
    from .models import IdeaTemplate

    labels = dict(IdeaTemplate.Network.choices)
    result = []
    for network in networks_in_account_order(accounts):
        if network not in (template.network_formats or {}) and network not in (template.networks or []):
            continue
        offered = template_network_formats(network)
        picked = (template.network_formats or {}).get(network, [])
        if len(offered) > 1 and picked:
            shorts = ", ".join(template_format_short(fmt, offered) for fmt in offered if fmt in picked)
            result.append(f"{labels[network]} ({shorts})")
        else:
            result.append(str(labels[network]))
    return result


def network_of_platform(platform) -> str:
    """The template network an account platform belongs to ("" for none)."""
    return next((network for network, platforms in TEMPLATE_NETWORK_PLATFORMS.items() if platform in platforms), "")


def template_account_formats(network_formats, accounts) -> dict[str, list[str]]:
    """``{"<account id>": [formats]}`` for the ``accounts`` on a template's
    networks: each on the formats the template names for its network that the
    account offers."""
    result = {}
    for account in accounts:
        wanted = (network_formats or {}).get(network_of_platform(account.platform), [])
        formats = [fmt for fmt in account.formats if fmt in wanted]
        if formats:
            result[str(account.id)] = formats
    return result


def platforms_for_template_networks(networks) -> set[str]:
    """The ``SocialAccount.platform`` values of a template's ``networks``."""
    return set().union(*(TEMPLATE_NETWORK_PLATFORMS.get(network, set()) for network in networks or []))


def template_variations(template) -> list[dict]:
    """The versions of a template's text, the template's own ``body`` first:
    ``[{"title", "body"}]``. Use with ``prefetch_related("variations")``."""
    versions = [{"title": "", "body": template.body or template.description}]
    versions += [{"title": item.title, "body": item.body} for item in template.variations.all()]
    return versions


def template_tags(template) -> list[str]:
    """The tags an idea or a task made from ``template`` gets: its category's name.
    Shown under "Suggested tags" in the template's panel."""
    from apps.common.validators import parse_and_truncate_tag_string

    return parse_and_truncate_tag_string(str(template.get_category_display()))


def template_text(template, variation) -> str:
    """The text of version ``variation`` (0 is the template's own), or the
    template's own when that version does not exist."""
    versions = template_variations(template)
    try:
        index = int(variation)
    except (TypeError, ValueError):
        index = 0
    return versions[index]["body"] if 0 <= index < len(versions) else versions[0]["body"]


def templates_for(workspace):
    """The templates a brand may use: the shelf (approved and shown) plus the
    ones it sent itself, which it uses at once, before or without approval."""
    from django.db.models import Q

    from .models import IdeaTemplate

    return IdeaTemplate.objects.filter(Q(is_active=True) | Q(submitted_workspace=workspace))


def template_shelf(workspace) -> list[dict]:
    """The active templates as the create page's Templates tab draws them,
    every label already in the user's language."""
    from . import idea_icons
    from .models import IdeaTemplate, IdeaTemplateFavorite

    favorites = set(IdeaTemplateFavorite.objects.for_workspace(workspace.id).values_list("template_id", flat=True))
    popular = popular_template_ids()
    formats = dict(IdeaTemplate.Format.choices)
    goals = dict(IdeaTemplate.Goal.choices)
    shelf = []
    reviews = dict(IdeaTemplate.Review.choices)
    accounts = list(channel_accounts(workspace))
    for template in templates_for(workspace).prefetch_related("variations"):
        shelf.append(
            {
                "id": template.id,
                "title": template.title,
                "description": template.description,
                "details": template.details or template.description,
                "category": template.category,
                "category_label": str(template.get_category_display()),
                "tone": idea_icons.category_style(template.category)[1],
                # What a version's "Create idea" opens the idea window with: the category's icon
                # (or the template's own) and the brand's accounts on its networks, on its formats.
                "icon": template.icon or idea_icons.category_style(template.category)[0],
                "channel_formats": template_account_formats(template.network_formats, accounts),
                "icon_markup": idea_icons.icon_markup(template.icon or idea_icons.category_style(template.category)[0]),
                "image": template.image.url if template.image else "",
                "formats": [str(formats[key]) for key in template.formats if key in formats],
                "goals": [str(goals[key]) for key in template.goals if key in goals],
                "networks": template_network_labels(template, accounts),
                "network_items": template_network_items(template, accounts),
                "ideal_for": template.ideal_for,
                "briefing": template.briefing,
                "art_items": creative_formats.art_items_payload(template.art_items),
                "tags": template_tags(template),
                "variations": template_variations(template),
                "favorite": template.id in favorites,
                # The brand sent it: listed under "My templates", with where its review stands.
                "mine": template.submitted_workspace_id == workspace.id,
                "review": template.review_status,
                "review_label": str(reviews.get(template.review_status, "")),
                "popular": template.id in popular,
            }
        )
    return shelf


def toggle_template_favorite(workspace, template) -> bool:
    """Save ``template`` for the brand, or drop it if it was saved. Returns
    whether it is now a favorite."""
    from .models import IdeaTemplateFavorite

    deleted, _rows = IdeaTemplateFavorite.objects.filter(workspace=workspace, template=template).delete()
    if deleted:
        return False
    IdeaTemplateFavorite.objects.get_or_create(workspace=workspace, template=template)
    return True


#: How many templates a brand may have waiting for approval at once, and how
#: many more versions of the text one of them may bring.
MAX_PENDING_TEMPLATE_SUBMISSIONS = 10
MAX_SUBMITTED_VARIATIONS = 4


class TemplateSubmissionLimitError(Exception):
    """The brand already has ``MAX_PENDING_TEMPLATE_SUBMISSIONS`` templates waiting."""


def submit_template(workspace, user, template, variations=()):
    """A customer's template idea, kept hidden until the operator approves it.

    ``template`` is the unsaved ``IdeaTemplate`` the customer filled in
    (``forms.TemplateSubmissionForm().save(commit=False)``: title, texts,
    category, icon, cover, characteristics). ``variations`` are more versions of the text,
    ``(title, body)`` pairs; empty ones are skipped and at most
    ``MAX_SUBMITTED_VARIATIONS`` are kept.
    Raises ``TemplateSubmissionLimitError`` when the brand already has too many waiting.
    """
    from django.db import transaction

    from .models import IdeaTemplate, IdeaTemplateVariation

    waiting = IdeaTemplate.objects.filter(
        submitted_workspace=workspace, review_status=IdeaTemplate.Review.PENDING
    ).count()
    if waiting >= MAX_PENDING_TEMPLATE_SUBMISSIONS:
        raise TemplateSubmissionLimitError
    versions = [(name.strip()[:120], text.strip()) for name, text in variations if text and text.strip()]
    with transaction.atomic():
        template.is_active = False
        template.review_status = IdeaTemplate.Review.PENDING
        template.submitted_by = user
        template.submitted_workspace = workspace
        template.save()
        IdeaTemplateVariation.objects.bulk_create(
            IdeaTemplateVariation(template=template, title=name, body=text, position=index)
            for index, (name, text) in enumerate(versions[:MAX_SUBMITTED_VARIATIONS])
        )
    return template


def review_template(template, approve: bool) -> None:
    """The operator's answer to a sent template: approving puts it on every
    brand's shelf; rejecting keeps it hidden."""
    from .models import IdeaTemplate

    template.review_status = IdeaTemplate.Review.APPROVED if approve else IdeaTemplate.Review.REJECTED
    template.is_active = approve
    template.save(update_fields=["review_status", "is_active", "updated_at"])


def record_template_use(workspace, template) -> None:
    """Count one use of ``template`` by the brand (for the popular ones)."""
    from .models import IdeaTemplateUse

    IdeaTemplateUse.objects.create(workspace=workspace, template=template)


def ensure_tags(workspace, names) -> None:
    """Save in the brand's tag list (``Tag``) every name not there yet.

    Ideas, posts and tasks share that list: it is where their tag pickers suggest from.
    """
    from .models import Tag

    names = [name for name in names if name]
    if not names:
        return
    existing = set(Tag.objects.for_workspace(workspace.id).filter(name__in=names).values_list("name", flat=True))
    new_tags = [Tag(workspace=workspace, name=name) for name in dict.fromkeys(names) if name not in existing]
    if new_tags:
        Tag.objects.bulk_create(new_tags, ignore_conflicts=True)


def tag_usage(workspace) -> list[dict]:
    """The brand's saved tags by name, each with how many ideas and posts carry it.

    ``[{"name", "kind", "ideas", "posts"}]``.
    """
    from collections import Counter

    from .models import Idea, Post, Tag

    ideas = Counter(
        name for tags in Idea.objects.for_workspace(workspace.id).values_list("tags", flat=True) for name in tags or []
    )
    posts = Counter(
        name for tags in Post.objects.for_workspace(workspace.id).values_list("tags", flat=True) for name in tags or []
    )
    return [
        {"name": tag.name, "kind": tag.kind, "ideas": ideas[tag.name], "posts": posts[tag.name]}
        for tag in Tag.objects.for_workspace(workspace.id)
    ]


def delete_tag(workspace, name) -> bool:
    """Delete the brand's tag ``name`` and take it off its ideas and posts.

    Leaving it on them would bring it back: saving one of those ideas or posts
    recreates the tags it carries. Its groups lose it with the row. Returns
    whether the tag existed.
    """
    from django.db import transaction

    from .models import Idea, Post, Tag

    with transaction.atomic():
        deleted, _rows = Tag.objects.for_workspace(workspace.id).filter(name=name).delete()
        for model in (Idea, Post):
            for item in model.objects.for_workspace(workspace.id).filter(tags__contains=[name]):
                item.tags = [tag for tag in item.tags if tag != name]
                item.save(update_fields=["tags"])
    return bool(deleted)


# ---------------------------------------------------------------------------
# Channels an idea or a task is meant for
# ---------------------------------------------------------------------------


def channel_accounts(workspace):
    """The brand's accounts content can be meant for: the connected ones it can publish to."""
    from apps.social_accounts.models import SocialAccount

    return (
        SocialAccount.objects.for_workspace(workspace.id)
        .publishable()
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .in_display_order()
    )


def parse_channel_formats(raw) -> dict:
    """A posted ``channel_formats`` field (JSON ``{"<account id>": [formats]}``), or {}."""
    import json

    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def clean_channel_formats(workspace, picked) -> dict:
    """``picked`` (``{"<account id>": [formats]}``) kept to the brand's connected accounts,
    each on the formats it offers among those asked; accounts left without one are dropped."""
    picked = picked if isinstance(picked, dict) else {}
    ids = []
    for key in picked:
        try:
            ids.append(str(uuid.UUID(str(key))))
        except ValueError:
            continue
    cleaned = {}
    for account in channel_accounts(workspace).filter(id__in=ids):
        asked = picked.get(str(account.id)) or []
        formats = [fmt for fmt in account.formats if fmt in asked]
        if formats:
            cleaned[str(account.id)] = formats
    return cleaned
