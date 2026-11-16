"""Queue scheduling services for the Content Calendar (F-2.3)."""

import zoneinfo
from datetime import datetime, time, timedelta

from django.utils import timezone
from django.utils.dates import WEEKDAYS
from django.utils.formats import date_format
from django.utils.text import capfirst
from django.utils.translation import gettext as _

from .models import PostingSlot, Queue, QueueEntry


def weekday_names():
    """Localized weekday names, Monday first, for calendar grid headers."""
    return [WEEKDAYS[i] for i in range(7)]


def month_label(day):
    """Localized "November 2026" / "Novembro de 2026" heading for a month view."""
    return capfirst(date_format(day, "YEAR_MONTH_FORMAT"))


def month_options(year, selected_month):
    """The twelve months of ``year`` for the toolbar's month picker, January first."""
    from datetime import date

    return [
        {
            "date": date(year, month, 1),
            "label": capfirst(date_format(date(year, month, 1), "F")),
            "active": month == selected_month,
        }
        for month in range(1, 13)
    ]


def week_label(first, last):
    """Localized range heading for a week view.

    A week inside one month is short ("November 9–15, 2026"); one across two months
    names both ("Nov. 30 – Dec. 6, 2026").
    """
    if (first.year, first.month) == (last.year, last.month):
        return _("%(month)s %(first)s–%(last)s, %(year)s") % {
            "month": date_format(first, "F"),
            "first": first.day,
            "last": last.day,
            "year": first.year,
        }
    return f"{date_format(first, 'MONTH_DAY_FORMAT')} – {date_format(last, 'DATE_FORMAT')}"


def day_label(day):
    """Localized heading for a day view, e.g. "Friday, Nov. 6, 2026"."""
    return f"{capfirst(date_format(day, 'l'))}, {date_format(day, 'DATE_FORMAT')}"


# Default posting slots created automatically for newly connected channels.
DEFAULT_POSTING_SLOTS = {
    0: [time(9, 24), time(10, 10), time(11, 26), time(12, 42)],  # Monday
    1: [time(9, 55), time(10, 41), time(11, 57), time(12, 13)],  # Tuesday
    2: [time(9, 30), time(10, 17), time(11, 32), time(12, 41)],  # Wednesday
    3: [time(9, 38), time(10, 52)],  # Thursday
}


def create_default_queue_and_slots(social_account):
    """Create a default Queue and PostingSlots for a newly connected social account.

    Skips creation if the account already has a queue (e.g. on re-connection).
    """
    if Queue.objects.filter(social_account=social_account).exists():
        return None

    queue = Queue.objects.create(
        workspace=social_account.workspace,
        name=f"{social_account.account_name or social_account.account_handle} Queue",
        social_account=social_account,
    )

    slots = []
    for day, times in DEFAULT_POSTING_SLOTS.items():
        for t in times:
            slots.append(PostingSlot(social_account=social_account, day_of_week=day, time=t))
    PostingSlot.objects.bulk_create(slots, ignore_conflicts=True)

    return queue


def _next_slot_datetimes(social_account, after_dt, count=30):
    """Compute the next `count` PostingSlot datetimes for a social account.

    Starting from `after_dt`, walks forward through the week to find
    upcoming slot times based on the account's PostingSlot configuration.

    Slot times are naive wall-clock times in the account's workspace timezone
    (see ``PostingSlot.time``), so they are resolved in that zone regardless of
    the tzinfo carried by ``after_dt`` — the caller's baseline only sets the
    "not before" instant. ``after_dt`` is always timezone-aware (callers pass
    ``timezone.now()`` or a tz-aware floor).
    """
    slots = PostingSlot.objects.filter(social_account=social_account, is_active=True).order_by("day_of_week", "time")
    if not slots.exists():
        return []

    ws_tz = zoneinfo.ZoneInfo(social_account.workspace.effective_timezone or "UTC")
    after_local = after_dt.astimezone(ws_tz)

    slot_list = list(slots)
    results = []
    current_date = after_local.date()

    # Walk up to 60 days forward to find enough slots
    for day_offset in range(60):
        check_date = current_date + timedelta(days=day_offset)
        weekday = check_date.weekday()  # 0=Monday

        for slot in slot_list:
            if slot.day_of_week != weekday:
                continue

            # Interpret the slot's wall-clock time in the workspace zone (DST
            # offsets resolve per-date), then compare as instants (both aware).
            slot_dt = datetime.combine(check_date, slot.time).replace(tzinfo=ws_tz)
            if slot_dt <= after_dt:
                continue

            results.append(slot_dt)
            if len(results) >= count:
                return results

    return results


# ---------------------------------------------------------------------------
# Stable slot-occupancy model
#
# A queue's "slots" are the upcoming PostingSlot datetimes for its channel; each
# is either OCCUPIED (a non-published post on the channel already holds that
# instant) or a GAP. Operations are LOCAL — they fill or vacate a single slot
# and never recompute unrelated entries — so an entry's time stays stable until
# that entry is explicitly moved. Occupancy is keyed off PlatformPost.scheduled_at
# (the publisher's source of truth), not the queue rows, so a manually-scheduled
# post or a recurrence clone on the same channel also reserves its instant — no
# double-booking. PROTECTED_STATUSES (published/publishing) are history and never
# occupy a future candidate or get moved.
# ---------------------------------------------------------------------------


class QueueFullError(Exception):
    """No open posting slot exists within the scheduling lookahead horizon."""


def _occupied_datetimes(social_account, *, exclude_pp_ids=()):
    """Future slot instants already claimed on this channel.

    Non-published/-publishing PlatformPosts with a future ``scheduled_at`` on
    ``social_account``. ``exclude_pp_ids`` drops a post's own row so it never
    blocks itself while being re-slotted.
    """
    from apps.composer.models import PlatformPost

    qs = (
        PlatformPost.objects.filter(
            social_account=social_account,
            scheduled_at__isnull=False,
            scheduled_at__gt=timezone.now(),
        )
        .exclude(status__in=PlatformPost.PROTECTED_STATUSES)
        .exclude(id__in=list(exclude_pp_ids))
    )
    return set(qs.values_list("scheduled_at", flat=True))


def _next_available_slot(social_account, *, exclude_pp_ids=(), after=None):
    """First upcoming PostingSlot datetime not already occupied, or ``None``.

    ``None`` means every slot within the lookahead horizon (``_next_slot_datetimes``
    caps at 60 days) is taken — the queue is full.
    """
    after = after or timezone.now()
    occupied = _occupied_datetimes(social_account, exclude_pp_ids=exclude_pp_ids)
    # Among the first ``len(occupied) + 1`` distinct slots at most ``len(occupied)``
    # can be taken, so one is guaranteed free if any free slot exists at all.
    candidates = _next_slot_datetimes(social_account, after, count=len(occupied) + 1)
    for slot_dt in candidates:
        if slot_dt not in occupied:
            return slot_dt
    return None


def _platform_post_for(post, social_account):
    """The post's representative PlatformPost on this channel (or ``None``).

    A post can go out in several formats on one account (a Feed post and a
    Story); they share the account's queue slot, so the queue works on one row
    and ``_write_slot`` carries the slot to the others. The row returned is a
    movable one when there is any, so it reads as protected only when every
    format already published.
    """
    from apps.composer.models import PlatformPost

    rows = list(post.platform_posts.filter(social_account=social_account))
    if not rows:
        return None
    rows.sort(key=lambda p: (p.status in PlatformPost.PROTECTED_STATUSES, p.format != PlatformPost.Format.DEFAULT))
    return rows[0]


def _movable_formats(pp):
    """``pp`` and the other formats of its post on the same account that may still move."""
    from apps.composer.models import PlatformPost

    if pp is None:
        return PlatformPost.objects.none()
    return PlatformPost.objects.filter(post_id=pp.post_id, social_account_id=pp.social_account_id).exclude(
        status__in=PlatformPost.PROTECTED_STATUSES
    )


def _slot_holder_ids(pp):
    """Rows to leave out of occupancy while re-slotting ``pp``: all its movable formats."""
    if pp is None:
        return []
    return [pp.id, *_movable_formats(pp).exclude(id=pp.id).values_list("id", flat=True)]


def drop_queue_entry_unless_scheduled(post_id, social_account_id):
    """Free the account's queue slot for this post, unless another format still needs it.

    Call once the row that left the schedule (published, drafted, deleted) has
    been saved: a post's formats on one account share one QueueEntry, so it
    goes only when none of them is still ``scheduled``.
    """
    from apps.composer.models import PlatformPost

    if PlatformPost.objects.filter(
        post_id=post_id,
        social_account_id=social_account_id,
        status=PlatformPost.Status.SCHEDULED,
    ).exists():
        return
    QueueEntry.objects.filter(post_id=post_id, queue__social_account_id=social_account_id).delete()


def _is_protected(pp):
    from apps.composer.models import PlatformPost

    return pp is not None and pp.status in PlatformPost.PROTECTED_STATUSES


def _lock_channel(queue):
    """Serialize slot writes for a channel within the current transaction.

    Occupancy is computed from ``PlatformPost`` rows (the publisher's truth),
    which a per-queue ``select_for_update`` on QueueEntry rows does not cover —
    two concurrent ops on *different* queues of the same channel would otherwise
    read the same free slot and double-book. Locking the ``SocialAccount`` row
    makes every slot op on a channel mutually exclusive.
    """
    from apps.social_accounts.models import SocialAccount

    list(SocialAccount.objects.select_for_update().filter(id=queue.social_account_id))


def _ensure_entry(post, queue):
    """Return the post's QueueEntry, creating it (appended) if absent."""
    from django.db.models import Max

    entry = queue.entries.filter(post=post).first()
    if entry is None:
        max_pos = queue.entries.aggregate(m=Max("position"))["m"]
        entry = QueueEntry.objects.create(queue=queue, post=post, position=(max_pos or 0) + 1)
    return entry


def _write_slot(entry, pp, slot_dt):
    """Persist one slot assignment to the QueueEntry and its PlatformPost."""
    from apps.composer.services import sync_post_scheduled_at

    entry.assigned_slot_datetime = slot_dt
    entry.save(update_fields=["assigned_slot_datetime"])
    if pp is not None:
        pp.scheduled_at = slot_dt
        pp.save(update_fields=["scheduled_at", "updated_at"])
        _movable_formats(pp).exclude(id=pp.id).update(scheduled_at=slot_dt, updated_at=timezone.now())
        sync_post_scheduled_at(pp.post)


def add_post_next_available(post, queue):
    """Place ``post`` into this queue's first open slot (upsert-aware).

    Comment §1 (Create · Next Available) and §4 (Edit · Next Available): if the
    post is already queued, its own slot is vacated first and it is re-slotted to
    the next gap; existing entries are never disturbed. Raises ``QueueFullError`` when
    no slot is free within the horizon.
    """
    from django.db import transaction

    with transaction.atomic():
        _lock_channel(queue)

        pp = _platform_post_for(post, queue.social_account)
        # A published/publishing post's schedule is history — never re-slot it
        # (mirrors the guard in prioritize/reorder; protects the reslot endpoint).
        if _is_protected(pp):
            return queue.entries.filter(post=post).first()
        exclude = _slot_holder_ids(pp)
        slot_dt = _next_available_slot(queue.social_account, exclude_pp_ids=exclude)
        if slot_dt is None:
            raise QueueFullError(f"Queue {queue.id} has no open slot within the scheduling horizon.")

        entry = _ensure_entry(post, queue)
        _write_slot(entry, pp, slot_dt)
        return entry


def prioritize(post, queue):
    """Place ``post`` at the queue's earliest slot, laddering others up by one.

    Comment §2: if the earliest upcoming slot is free, ``post`` takes it and
    nothing moves. Otherwise every other occupied queue entry shifts one slot
    later (``[k]→[k+1]``, preserving the gap pattern) before ``post`` takes
    slot ``[0]``. Upsert-aware. Raises ``QueueFullError`` past the horizon.
    """
    from django.db import transaction

    with transaction.atomic():
        _lock_channel(queue)

        now = timezone.now()
        pp = _platform_post_for(post, queue.social_account)

        # This queue's movable entries (exclude `post`, exclude protected), each
        # keyed on the ``PlatformPost.scheduled_at`` the publisher fires on — NOT
        # ``QueueEntry.assigned_slot_datetime``, which a manual calendar drag via
        # ``reschedule_post`` can leave stale and divergent (then the ladder would
        # mis-place the post or double-book a slot another post really holds).
        movable = []  # (entry, pp, current_slot_dt)
        for e in queue.entries.exclude(post=post).select_related("post"):
            p = _platform_post_for(e.post, queue.social_account)
            if p is None or _is_protected(p) or p.scheduled_at is None or p.scheduled_at <= now:
                continue
            movable.append((e, p, p.scheduled_at))
        movable_dts = {dt for _, _, dt in movable}

        candidates = _next_slot_datetimes(queue.social_account, now, count=len(movable) + 2)
        if not candidates:
            raise QueueFullError(f"Queue {queue.id} has no posting slots within the horizon.")

        entry = _ensure_entry(post, queue)
        target = candidates[0]

        # Slot 0 is free among the queue's own entries.
        if target not in movable_dts:
            # If a foreign/fixed post (manual schedule, clone, other queue) holds
            # slot 0, we cannot preempt it — degrade to the next genuine gap.
            occupied = _occupied_datetimes(queue.social_account, exclude_pp_ids=_slot_holder_ids(pp))
            if target in occupied:
                free = _next_available_slot(queue.social_account, exclude_pp_ids=_slot_holder_ids(pp))
                if free is None:
                    raise QueueFullError(f"Queue {queue.id} has no open slot within the scheduling horizon.")
                _write_slot(entry, pp, free)
                return entry
            _write_slot(entry, pp, target)
            return entry

        # Slot 0 taken by a queue entry: ladder every movable entry up to its
        # next free slot. Each mover lands on the earliest candidate after its
        # current index that is neither held by a post we can't move (foreign /
        # manual / out-of-horizon) nor already claimed by another mover — so the
        # ladder never double-books and never runs off the lookahead horizon.
        index_of = {dt: i for i, dt in enumerate(candidates)}
        movers = [(index_of[dt], e, p) for e, p, dt in movable if dt in index_of]
        mover_old_dts = {dt for _, _, dt in movable if dt in index_of}
        # Datetimes that must stay free: everything occupied on this channel
        # except the movers we are about to relocate (their slots are vacated).
        avoid_dts = _occupied_datetimes(queue.social_account, exclude_pp_ids=_slot_holder_ids(pp)) - mover_old_dts

        assigned_idx = {0}  # slot 0 is reserved for the prioritized post
        # Descending so a destination is always vacated before it is written.
        for cur_idx, e, p in sorted(movers, key=lambda t: t[0], reverse=True):
            j = cur_idx + 1
            while True:
                if j >= len(candidates):
                    candidates = _next_slot_datetimes(queue.social_account, now, count=j + 2)
                    if j >= len(candidates):
                        raise QueueFullError(f"Queue {queue.id} has no open slot within the scheduling horizon.")
                if j not in assigned_idx and candidates[j] not in avoid_dts:
                    break
                j += 1
            assigned_idx.add(j)
            _write_slot(e, p, candidates[j])
        _write_slot(entry, pp, candidates[0])
        return entry


def remove_from_queue(entry):
    """Remove a single entry, leaving a gap; neighbours are untouched.

    Comment §3: delete the QueueEntry and clear the matching PlatformPost's
    schedule. The child is transitioned ``scheduled→draft`` (which nulls
    ``scheduled_at``) rather than nulled directly, so it can't become instantly
    due via the publisher's ``Coalesce(scheduled_at, post__scheduled_at)``
    fallback while the parent aggregate still points at a past time.
    """
    from django.db import transaction

    from apps.composer.services import sync_post_scheduled_at, transition_platform_post

    with transaction.atomic():
        post = entry.post
        queue = entry.queue
        _lock_channel(queue)
        pp = _platform_post_for(post, queue.social_account)
        # Clear the schedule for any non-protected child (scheduled OR failed),
        # not just 'scheduled', so a removed failed post stops rendering on the
        # calendar at its past attempt time. Published/publishing stay (history).
        if pp is not None and not _is_protected(pp):
            for row in _movable_formats(pp):
                if row.can_transition_to("draft"):
                    transition_platform_post(row, "draft")
        entry.delete()
        sync_post_scheduled_at(post)


def reslot_to_next_available(entry):
    """Free this entry's slot and move it to the queue's next gap."""
    return add_post_next_available(entry.post, entry.queue)


def add_to_queue(post, queue, priority=False):
    """Add a post to a queue (back-compat dispatcher).

    Routes to the stable-slot ops: ``prioritize`` (top of queue) or
    ``add_post_next_available`` (first open gap). Kept so existing callers and
    the composer's ``add_to_queue`` / ``add_to_queue_priority`` actions only
    need their resolved service swapped, not their wiring.
    """
    if priority:
        return prioritize(post, queue)
    return add_post_next_available(post, queue)


def reorder_queue(queue, ordered_entry_ids):
    """Reassign the queue's occupied slot times to entries in a new order.

    Drag-reorder under the slot model: the SET of occupied datetimes is
    preserved (gaps stay put); only which post sits in which slot changes. The
    movable, slotted entries named in ``ordered_entry_ids`` are matched — in that
    visual order — to their datetimes sorted ascending. Protected
    (published/publishing) entries are immovable and skipped.
    """
    from django.db import transaction

    with transaction.atomic():
        _lock_channel(queue)

        ordered = []
        for eid in ordered_entry_ids:
            e = queue.entries.filter(id=eid).select_related("post").first()
            if e is None:
                continue
            p = _platform_post_for(e.post, queue.social_account)
            if _is_protected(p):
                continue
            ordered.append((e, p))

        # Redistribute the slot instants already held — keyed on the publisher's
        # ``PlatformPost.scheduled_at`` (not ``assigned_slot_datetime``, which a
        # manual drag can leave stale) — among the entries that hold one. Never
        # invent or drop a slot during a reorder.
        slotted = [(e, p) for e, p in ordered if p is not None and p.scheduled_at is not None]
        slots = sorted(p.scheduled_at for _, p in slotted)
        for idx, (e, p) in enumerate(slotted):
            _write_slot(e, p, slots[idx])

        # Keep ``position`` as a stable visual tie-break in the dragged order.
        for idx, (e, _p) in enumerate(ordered):
            if e.position != idx:
                QueueEntry.objects.filter(id=e.id).update(position=idx)


def repair_future_published_scheduled_at(*, workspace_id=None, apply=True):
    """Reset ``scheduled_at`` for published posts dragged into the future.

    Before the ``assign_queue_slots`` guard landed, re-running a queue (adding or
    reordering a post) re-slotted *every* entry — including already-published
    ones — pushing their ``scheduled_at`` onto a future posting slot. Because the
    calendar places chips by ``scheduled_at``, those posts then showed as
    "published" up to a week ahead.

    A correctly-published post always has ``scheduled_at <= published_at`` (you
    schedule, then it fires at or after that instant). ``scheduled_at >
    published_at`` is therefore an unambiguous signature of this corruption, so
    the repair targets exactly those rows and resets ``scheduled_at`` to the true
    ``published_at``. Idempotent and safe to re-run: once reset, a row no longer
    matches the filter.

    The same bad future slot was also written to the matching
    ``QueueEntry.assigned_slot_datetime`` (the queue the bug walked, where
    ``queue.social_account == platform_post.social_account``). ``queue_detail``
    renders that field and the published-status guard stops ``assign_queue_slots``
    from ever recomputing a published entry, so the repair snaps that stale queue
    timestamp back to ``published_at`` too (same ``> published_at`` signature).

    Returns a summary dict ``{"rows": [...], "platform_post_count": int,
    "post_count": int, "queue_entry_count": int, "applied": bool}`` where each
    row is a plain dict describing one affected ``PlatformPost`` (for dry-run
    reporting).
    """
    from django.db.models import F

    from apps.composer.models import PlatformPost, Post
    from apps.composer.services import sync_post_scheduled_at

    affected = (
        PlatformPost.objects.filter(
            status=PlatformPost.Status.PUBLISHED,
            published_at__isnull=False,
            scheduled_at__isnull=False,
            scheduled_at__gt=F("published_at"),
        )
        .select_related("social_account", "post")
        .order_by("scheduled_at")
    )
    if workspace_id is not None:
        affected = affected.filter(post__workspace_id=workspace_id)

    rows = []
    post_ids = set()
    queue_targets = []
    for pp in affected:
        rows.append(
            {
                "platform_post_id": str(pp.id),
                "post_id": str(pp.post_id),
                "workspace_id": str(pp.post.workspace_id),
                "platform": pp.social_account.platform,
                "account": pp.social_account.account_name or pp.social_account.account_handle,
                "old_scheduled_at": pp.scheduled_at,
                "new_scheduled_at": pp.published_at,
            }
        )
        post_ids.add(pp.post_id)
        queue_targets.append((pp.post_id, pp.social_account_id, pp.published_at))

    # The bug stamped the same future slot onto the matching QueueEntry
    # (queue.social_account == pp.social_account); queue_detail renders it, and
    # the published-status guard now keeps assign_queue_slots from ever
    # recomputing a published entry. Snap those stale timestamps back too — same
    # ``> published_at`` signature, so a correctly null/past entry is left alone.
    queue_entry_targets = [
        (
            published_at,
            QueueEntry.objects.filter(
                post_id=post_id,
                queue__social_account_id=social_account_id,
                assigned_slot_datetime__gt=published_at,
            ),
        )
        for post_id, social_account_id, published_at in queue_targets
    ]
    queue_entry_count = 0

    if apply and rows:
        from django.db import transaction

        with transaction.atomic():
            posts = list(Post.objects.filter(id__in=post_ids))
            # Snapping a published child back to its past ``published_at`` lowers
            # the parent ``Post.scheduled_at`` aggregate (min-of-children) into
            # the past. A SCHEDULED sibling with ``scheduled_at=NULL`` resolves
            # its due time through the publisher's
            # ``Coalesce(scheduled_at, post__scheduled_at)`` fallback, so a
            # backward parent move would make that sibling instantly due and
            # publish it early. Pin such siblings to their current effective time
            # *before* lowering the parent, so the repair never drags a pending
            # post's schedule into the past.
            for post in posts:
                if post.scheduled_at is not None:
                    post.platform_posts.filter(
                        status=PlatformPost.Status.SCHEDULED,
                        scheduled_at__isnull=True,
                    ).update(scheduled_at=post.scheduled_at)
            # Snap each corrupt published child back to its real publish instant,
            # then recompute the parent aggregate so listings and Coalesce
            # fallbacks line up again.
            affected.update(scheduled_at=F("published_at"))
            for published_at, stale_entries in queue_entry_targets:
                queue_entry_count += stale_entries.update(assigned_slot_datetime=published_at)
            for post in posts:
                sync_post_scheduled_at(post)
    else:
        # Dry run: report how many stale QueueEntry rows would be reset.
        for _published_at, stale_entries in queue_entry_targets:
            queue_entry_count += stale_entries.count()

    return {
        "rows": rows,
        "platform_post_count": len(rows),
        "post_count": len(post_ids),
        "queue_entry_count": queue_entry_count,
        "applied": bool(apply and rows),
    }


# ---------------------------------------------------------------------------
# Custom events and special dates
# ---------------------------------------------------------------------------

SPECIAL_DATES_SETTING = "calendar.show_special_dates"
CUSTOM_EVENTS_SETTING = "calendar.show_custom_events"
DRAFTS_SETTING = "calendar.show_drafts"


def events_by_date(workspace, start, end) -> dict:
    """``{day: [event, ...]}`` for every day an event (or a repetition of one) covers.

    Each listed event is a copy whose ``start_date``/``end_date`` are those of
    the occurrence, so templates don't need to know about recurrence; the
    series' own dates stay in ``series_start``/``series_end`` (what editing it changes).
    """
    import copy

    from django.db.models import Q

    from .models import CustomCalendarEvent

    events = (
        CustomCalendarEvent.objects.for_workspace(workspace.id)
        .filter(start_date__lte=end)
        .filter(Q(end_date__gte=start) | ~Q(recurrence=CustomCalendarEvent.Recurrence.NONE))
        .order_by("start_date")
    )
    by_day: dict = {}
    for event in events:
        for first, last in event.occurrences(start, end):
            occurrence = copy.copy(event)
            occurrence.series_start, occurrence.series_end = event.start_date, event.end_date
            occurrence.start_date, occurrence.end_date = first, last
            day = max(first, start)
            while day <= min(last, end):
                by_day.setdefault(day, []).append(occurrence)
                day += timedelta(days=1)
    return by_day


def special_dates_enabled(workspace) -> bool:
    from apps.settings_manager.helpers import get_setting

    return bool(get_setting(workspace.id, SPECIAL_DATES_SETTING, workspace_org_id=workspace.organization_id))


def drafts_shown(workspace) -> bool:
    """Whether the calendar shows draft posts (the toolbar's "Drafts" button)."""
    from apps.settings_manager.helpers import get_setting

    return bool(get_setting(workspace.id, DRAFTS_SETTING, workspace_org_id=workspace.organization_id))


def custom_events_enabled(workspace) -> bool:
    """Whether the calendar shows the workspace's campaigns (its own CustomCalendarEvents)."""
    from apps.settings_manager.helpers import get_setting

    return bool(get_setting(workspace.id, CUSTOM_EVENTS_SETTING, workspace_org_id=workspace.organization_id))


def upcoming_dates(workspace, start, days=60) -> list[dict]:
    """Special dates (when shown) and the workspace's own events from ``start``, by date.

    ``[{"date", "days", "name", "emoji", "event"}]``: ``days`` from ``start``;
    ``event`` is the ``CustomCalendarEvent`` for the workspace's own dates, else None.
    """
    from .holidays import special_dates_between

    end = start + timedelta(days=days)
    entries = []
    if special_dates_enabled(workspace):
        entries.extend({**d, "event": None} for d in special_dates_between(start, end, workspace))
    seen = set()
    for _day, events in sorted(events_by_date(workspace, start, end).items()):
        for event in events:
            key = (event.pk, event.start_date)
            if key in seen:
                continue
            seen.add(key)
            entries.append({"date": max(event.start_date, start), "name": event.title, "emoji": "", "event": event})
    for entry in entries:
        entry["days"] = (entry["date"] - start).days
    return sorted(entries, key=lambda e: e["date"])


def date_segments_for(workspace) -> list[dict]:
    """Every segment on offer, with whether ``workspace`` follows it:
    ``[{"segment", "chosen", "date_count"}]``, by name."""
    from django.db.models import Count, Q

    from .models import DateSegment, WorkspaceDateSegment

    chosen = set(WorkspaceDateSegment.objects.for_workspace(workspace.id).values_list("segment_id", flat=True))
    segments = DateSegment.objects.filter(is_active=True).annotate(
        date_count=Count("dates", filter=Q(dates__is_active=True))
    )
    return [{"segment": s, "chosen": s.id in chosen, "date_count": s.date_count} for s in segments]


def toggle_date_segment(workspace, segment) -> bool:
    """Follow ``segment`` if the brand does not, stop if it does. Returns whether it now does."""
    from .models import WorkspaceDateSegment

    deleted, _rows = WorkspaceDateSegment.objects.filter(workspace=workspace, segment=segment).delete()
    if deleted:
        return False
    WorkspaceDateSegment.objects.get_or_create(workspace=workspace, segment=segment)
    return True


def special_dates_ahead(workspace, start, days=365) -> list[dict]:
    """The special dates of the coming year for the brand, whether or not the
    calendar is showing them: the global ones and those of its segments.

    Shaped for the special-dates tab, which draws them in the browser: every
    value is a string, a list of strings or the ``favorite`` flag, already in
    the user's language (``day``, ``month``, ``full_date``, ``when``,
    ``kind_label``). ``icon_markup`` is the inner markup of the date's icon,
    from the app's own library.
    """
    from django.utils.formats import date_format
    from django.utils.translation import gettext, ngettext

    from apps.composer.idea_icons import icon_markup

    from .holidays import special_dates_between
    from .models import FavoriteSpecialDate, SpecialDate

    kind_labels = dict(SpecialDate.Kind.choices)
    favorites = set(FavoriteSpecialDate.objects.for_workspace(workspace.id).values_list("key", flat=True))
    entries = []
    for entry in special_dates_between(start, start + timedelta(days=days), workspace):
        ahead = (entry["date"] - start).days
        entries.append(
            {
                "key": entry["key"],
                "favorite": entry["key"] in favorites,
                "image": entry["image"],
                "iso": entry["date"].isoformat(),
                "day": f"{entry['date'].day:02d}",
                "month": date_format(entry["date"], "M"),
                "full_date": date_format(entry["date"], "DATE_FORMAT"),
                "when": gettext("today")
                if ahead == 0
                else ngettext("in %(days)s day", "in %(days)s days", ahead) % {"days": ahead},
                "name": entry["name"],
                "icon": entry["icon"],
                "icon_markup": icon_markup(entry["icon"]),
                "kind": entry["kind"],
                "kind_label": str(kind_labels.get(entry["kind"], "")),
                "segment": entry["segment"],
                "description": entry["description"],
                "suggestions": entry["suggestions"],
                "tags": entry["tags"],
            }
        )
    return entries


def special_date_categories() -> list[dict]:
    """The filters of the special-dates tab: each category of date, then the
    dates that came from one of the brand's segments and the ones it saved."""
    from django.utils.translation import gettext

    from .models import SpecialDate

    categories = [{"key": key, "label": str(label)} for key, label in SpecialDate.Kind.choices]
    categories.append({"key": "segment", "label": gettext("From your segments")})
    categories.append({"key": "favorite", "label": gettext("Favorites")})
    return categories


def toggle_favorite_date(workspace, key) -> bool:
    """Save the special date ``key`` for the brand, or drop it if it was
    saved. Returns whether it is now a favorite."""
    from .models import FavoriteSpecialDate

    deleted, _rows = FavoriteSpecialDate.objects.filter(workspace=workspace, key=key).delete()
    if deleted:
        return False
    FavoriteSpecialDate.objects.get_or_create(workspace=workspace, key=key)
    return True


def set_date_segments(workspace, segment_ids) -> None:
    """Make the brand follow exactly these segments (those on offer among
    them); the others it followed are dropped."""
    from .models import DateSegment, WorkspaceDateSegment

    wanted = set(DateSegment.objects.filter(id__in=segment_ids, is_active=True).values_list("id", flat=True))
    current = set(WorkspaceDateSegment.objects.for_workspace(workspace.id).values_list("segment_id", flat=True))
    WorkspaceDateSegment.objects.filter(workspace=workspace, segment_id__in=current - wanted).delete()
    WorkspaceDateSegment.objects.bulk_create(
        WorkspaceDateSegment(workspace=workspace, segment_id=segment_id) for segment_id in wanted - current
    )
