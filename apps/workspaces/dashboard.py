"""Read-side services for the brand dashboard (workspaces.views.workspace_dashboard).

The dashboard only gathers: the analytics numbers come from
apps.analytics.services (the same calculations as the reports page), the
pending counts from the same statuses the sidebar badges use.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_noop

from apps.analytics import services as analytics
from apps.analytics.metrics import METRICS, PLATFORM_METRICS
from apps.composer.models import PlatformPost
from apps.social_accounts.models import SocialAccount


def reconnect_accounts(workspace) -> list[SocialAccount]:
    """The brand's accounts that lost their connection and need reconnecting."""
    return [
        account
        for account in SocialAccount.objects.publishable()
        .filter(workspace=workspace)
        .order_by("platform", "account_name")
        if account.needs_reconnect
    ]


def failed_post_count(workspace) -> int:
    """Posts with at least one network whose publication failed."""
    return (
        PlatformPost.objects.filter(post__workspace_id=workspace.id, status=PlatformPost.Status.FAILED)
        .values("post_id")
        .distinct()
        .count()
    )


def _month_bounds(today: date, tz) -> tuple[datetime, datetime]:
    first = today.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    start = timezone.make_aware(datetime.combine(first, time.min), tz)
    end = timezone.make_aware(datetime.combine(last + timedelta(days=1), time.min), tz)
    return start, end


def month_goal_progress(workspace, today: date, tz) -> dict[str, Any]:
    """Where the brand stands against its monthly post goal.

    Counts posts, not publications: one post sent to Instagram and Facebook
    counts once. ``published`` are posts with a network published this month;
    ``scheduled`` are the others with a network still scheduled for a later
    day of this month. ``pace`` says how the month is going, for the card's
    note: "done" (goal covered by published posts), "covered" (published +
    scheduled reach it), "on_track" (published keeps up with the days gone)
    or "behind"; ``None`` without a goal.
    """
    start, end = _month_bounds(today, tz)
    in_month = PlatformPost.objects.filter(post__workspace_id=workspace.id)
    published_ids = set(
        in_month.filter(
            status=PlatformPost.Status.PUBLISHED, published_at__gte=start, published_at__lt=end
        ).values_list("post_id", flat=True)
    )
    scheduled_ids = set(
        in_month.filter(status__in=(PlatformPost.Status.SCHEDULED, PlatformPost.Status.PUBLISHING))
        .filter(
            Q(scheduled_at__gte=start, scheduled_at__lt=end)
            | Q(scheduled_at__isnull=True, post__scheduled_at__gte=start, post__scheduled_at__lt=end)
        )
        .values_list("post_id", flat=True)
    )
    published = len(published_ids)
    scheduled = len(scheduled_ids - published_ids)
    goal = workspace.monthly_post_goal or None

    days_in_month = calendar.monthrange(today.year, today.month)[1]
    pace = None
    remaining = 0
    percent = 0
    if goal:
        remaining = max(goal - published - scheduled, 0)
        percent = min(round((published + scheduled) / goal * 100), 100)
        if published >= goal:
            pace = "done"
        elif published + scheduled >= goal:
            pace = "covered"
        elif published >= goal * today.day / days_in_month:
            pace = "on_track"
        else:
            pace = "behind"
    return {
        "goal": goal,
        "published": published,
        "scheduled": scheduled,
        "remaining": remaining,
        "percent": percent,
        "pace": pace,
        "days_left": days_in_month - today.day,
        "month": today.replace(day=1),
    }


def network_card(account: SocialAccount, days: int, end: date | None = None) -> dict[str, Any]:
    """One account's headline numbers for the "By network" row.

    ``stats`` holds up to three of them, chosen from what the platform itself
    reports: its audience total when it has one (subscribers on YouTube, none
    on Google Business), its primary metric, then the engagement rate when
    there is something to divide by, and otherwise the platform's next metrics
    (watch time on YouTube, clicks on Google Business). Each one carries
    ``derived`` — value and change against the previous period — except the
    audience total, which is a number of today. The period is the ``days`` days
    ending on ``end`` (today when ``None``).
    """
    # Set by analytics.services.analytics_accounts, which the accounts come from.
    if getattr(account, "analytics_verdict", None) is not None:
        return {"account": account, "available": False, "reason": getattr(account, "analytics_unavailable_reason", "")}
    catalog = PLATFORM_METRICS.get(account.platform, [])
    bundle = analytics.account_analytics_bundle(account, days, end)
    primary = analytics.hero_chart_data(account, days, bundle=bundle, end=end)
    engagement = analytics.engagement_card(account, days, bundle=bundle)

    stats: list[dict[str, Any]] = []
    audience = next((m for m in ("subscribers", "follows", "followers") if m in catalog), None)
    if audience:
        label = METRICS["subscribers" if audience == "subscribers" else "followers"]["label"]
        stats.append({"label": label, "value": account.follower_count, "derived": None})
    if primary["metric"]:
        stats.append({"label": primary["label"], "derived": primary["derived"]})
    if engagement and engagement["rate"].denominator:
        stats.append({"label": gettext_noop("Engagement"), "derived": engagement["rate"]})
    others = analytics.hero_cards(account, days, bundle=bundle, end=end) + (engagement["parts"] if engagement else [])
    stats.extend(
        {"label": other["label"], "derived": other["derived"]}
        for other in others
        if other["metric"] != primary["metric"]
    )
    return {"account": account, "available": True, "stats": stats[:3]}


def my_open_tasks(workspace, user, today: date, limit: int = 5) -> dict[str, Any]:
    """The tasks assigned to ``user`` that are not in a done column, most urgent
    first (overdue, then by due date; undated last). Each row carries ``due``:
    "overdue", "today", "upcoming" or ``None``. ``total`` counts them all."""
    from django.db.models import F

    from apps.tasks.models import Task

    open_tasks = Task.objects.for_workspace(workspace.id).filter(assignees=user, column__is_done=False)
    rows = []
    for task in open_tasks.order_by(F("due_date").asc(nulls_last=True), "-created_at")[:limit]:
        if task.due_date is None:
            due = None
        elif task.due_date < today:
            due = "overdue"
        elif task.due_date == today:
            due = "today"
        else:
            due = "upcoming"
        rows.append({"task": task, "due": due})
    return {"rows": rows, "total": open_tasks.count()}


# Statuses that mean the date already has its post on the way.
_READY_STATUSES = (
    PlatformPost.Status.APPROVED,
    PlatformPost.Status.SCHEDULED,
    PlatformPost.Status.PUBLISHING,
    PlatformPost.Status.PUBLISHED,
)


def upcoming_dates_with_posts(workspace, today: date, tz, limit: int = 4, fallback: int = 3) -> list[dict[str, Any]]:
    """The brand's events and special dates of the next 90 days, each with
    ``has_post``: whether a post is approved, scheduled or out on that day.

    Special dates come even when the calendar grid hides them: that switch is
    about the grid, and this card leads to the special-dates page ("See all"),
    which lists them anyway. When nothing falls in the next 90 days, the card
    offers the brand's next ``fallback`` special dates of the coming year, so it
    never sits empty while there is something to plan for."""
    from apps.calendar.holidays import special_dates_between
    from apps.calendar.services import special_dates_enabled, upcoming_dates

    entries = upcoming_dates(workspace, today, days=90)
    if not special_dates_enabled(workspace):
        entries += [
            {**d, "event": None, "days": (d["date"] - today).days}
            for d in special_dates_between(today, today + timedelta(days=90), workspace)
        ]
    entries = sorted(entries, key=lambda e: e["date"])[:limit]
    if not entries:
        entries = [
            {**d, "event": None, "days": (d["date"] - today).days}
            for d in special_dates_between(today, today + timedelta(days=365), workspace)[:fallback]
        ]
    if not entries:
        return []
    start = timezone.make_aware(datetime.combine(entries[0]["date"], time.min), tz)
    end = timezone.make_aware(datetime.combine(entries[-1]["date"] + timedelta(days=1), time.min), tz)
    moments = PlatformPost.objects.filter(post__workspace_id=workspace.id, status__in=_READY_STATUSES).filter(
        Q(published_at__gte=start, published_at__lt=end)
        | Q(scheduled_at__gte=start, scheduled_at__lt=end)
        | Q(scheduled_at__isnull=True, post__scheduled_at__gte=start, post__scheduled_at__lt=end)
    )
    days_with_posts = set()
    for published, scheduled, post_scheduled in moments.values_list(
        "published_at", "scheduled_at", "post__scheduled_at"
    ):
        moment = published or scheduled or post_scheduled
        if moment is not None:
            days_with_posts.add(timezone.localtime(moment, tz).date())
    return [{**entry, "has_post": entry["date"] in days_with_posts} for entry in entries]


def day_summary(workspace, user, perms: dict, today: date, tz, *, is_team: bool) -> dict[str, Any]:
    """What the header greets with: the part of the day, the person's first name
    and the day's counts, each only for who can act on it (``None`` otherwise):
    posts going out today (scheduled, publishing or already out today),
    pending approvals, unread messages and open tasks of theirs due today or
    overdue (only for the team, ``is_team``: clients have no task board).
    ``total`` adds up the ones shown."""
    from apps.approvals.services import pending_approval_count
    from apps.inbox.services import unread_count
    from apps.tasks.models import Task

    start, end = (timezone.make_aware(datetime.combine(d, time.min), tz) for d in (today, today + timedelta(days=1)))
    going_out = (
        PlatformPost.objects.filter(
            post__workspace_id=workspace.id,
            status__in=(PlatformPost.Status.SCHEDULED, PlatformPost.Status.PUBLISHING, PlatformPost.Status.PUBLISHED),
        )
        .filter(
            Q(published_at__gte=start, published_at__lt=end)
            | Q(published_at__isnull=True, scheduled_at__gte=start, scheduled_at__lt=end)
            | Q(
                published_at__isnull=True,
                scheduled_at__isnull=True,
                post__scheduled_at__gte=start,
                post__scheduled_at__lt=end,
            )
        )
        .values("post_id")
        .distinct()
        .count()
    )
    counts = {
        "posts": going_out,
        "approvals": pending_approval_count(workspace.id) if perms.get("approve_posts") else None,
        "messages": unread_count(workspace.id) if perms.get("use_inbox") else None,
        "tasks": (
            Task.objects.for_workspace(workspace.id)
            .filter(assignees=user, column__is_done=False, due_date__lte=today)
            .count()
            if is_team
            else None
        ),
    }
    hour = timezone.localtime(timezone=tz).hour
    return {
        "part_of_day": "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening",
        "first_name": (user.first_name or user.display_name).split()[0],
        **counts,
        "total": sum(v for v in counts.values() if v),
    }
