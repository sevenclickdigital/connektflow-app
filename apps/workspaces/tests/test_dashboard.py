"""The brand dashboard: who sees it, who sets the monthly goal, and the numbers
it adds up on its own (the month's goal, formats and best posting time)."""

import json
import zoneinfo
from datetime import date, datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.models import User
from apps.analytics.models import PostInsightsSnapshot
from apps.analytics.services import post_performance
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount
from apps.workspaces.dashboard import month_goal_progress
from apps.workspaces.models import Workspace

ROLE = WorkspaceMembership.WorkspaceRole
TZ = zoneinfo.ZoneInfo("America/Sao_Paulo")


@pytest.fixture
def brand(organization):
    return Workspace.objects.create(organization=organization, name="Brand", timezone="America/Sao_Paulo")


@pytest.fixture
def instagram(brand):
    return SocialAccount.objects.create(
        workspace=brand,
        platform="instagram",
        account_platform_id="ig-1",
        account_name="Brand IG",
        oauth_access_token="token",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _member(client, brand, role, email="member@example.com"):
    user = User.objects.create_user(email=email, password="x", name="Member", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=brand.organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(user=user, workspace=brand, workspace_role=role)
    client.force_login(user)
    return user


def _url(brand, name="workspaces:dashboard"):
    return reverse(name, kwargs={"workspace_id": brand.id})


def _platform_post(account, *, status, published_at=None, scheduled_at=None, post=None, fmt="default"):
    post = post or Post.objects.create(workspace=account.workspace, caption="hello")
    return PlatformPost.objects.create(
        post=post,
        social_account=account,
        status=status,
        published_at=published_at,
        scheduled_at=scheduled_at,
        format=fmt,
    )


# --- access -----------------------------------------------------------------


@pytest.mark.django_db
def test_a_member_sees_the_dashboard_with_its_networks(client, brand, instagram):
    AnalyticsPlatformConfig.objects.update_or_create(platform="instagram", defaults={"is_enabled": True})
    _member(client, brand, ROLE.VIEWER)

    page = client.get(_url(brand))
    response = client.get(_url(brand), {"partial": "networks"}, HTTP_HX_REQUEST="true")

    assert page.status_code == 200
    assert page.context["active_account"] == instagram
    assert "partial=networks" in page.content.decode()
    assert response.status_code == 200
    assert [card["account"] for card in response.context["network_cards"]] == [instagram]


@pytest.mark.django_db
def test_someone_from_another_brand_cannot_open_it(client, brand, organization):
    other = Workspace.objects.create(organization=organization, name="Other")
    _member(client, other, ROLE.OWNER)

    assert client.get(_url(brand)).status_code == 403


@pytest.mark.django_db
def test_an_account_of_another_brand_in_the_url_is_ignored(client, brand, instagram, organization):
    AnalyticsPlatformConfig.objects.update_or_create(platform="instagram", defaults={"is_enabled": True})
    other = Workspace.objects.create(organization=organization, name="Other")
    foreign = SocialAccount.objects.create(
        workspace=other,
        platform="instagram",
        account_platform_id="ig-2",
        account_name="Foreign IG",
        oauth_access_token="token",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    _member(client, brand, ROLE.OWNER)

    response = client.get(_url(brand), {"account": str(foreign.id)})

    assert response.context["active_account"] == instagram
    assert "Foreign IG" not in response.content.decode()


@pytest.mark.django_db
def test_without_reports_permission_no_network_numbers_are_shown(client, brand, instagram):
    AnalyticsPlatformConfig.objects.update_or_create(platform="instagram", defaults={"is_enabled": True})
    _member(client, brand, ROLE.CONTRIBUTOR)

    page = client.get(_url(brand))
    response = client.get(_url(brand), {"partial": "networks"}, HTTP_HX_REQUEST="true")

    assert page.status_code == 200
    assert page.context["active_account"] is None
    assert "partial=networks" not in page.content.decode()
    assert response.context["network_cards"] == []


# --- monthly goal -------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("role", [ROLE.OWNER, ROLE.MANAGER])
def test_owners_and_managers_set_the_goal(client, brand, role):
    _member(client, brand, role)

    response = client.post(
        _url(brand, "workspaces:dashboard_goal"), {"monthly_post_goal": "20"}, HTTP_HX_REQUEST="true"
    )

    assert response.status_code == 204
    triggers = json.loads(response.headers["HX-Trigger"])
    assert triggers["showToast"]["tone"] == "success"
    assert "dashboardGoalChanged" in triggers
    brand.refresh_from_db()
    assert brand.monthly_post_goal == 20


@pytest.mark.django_db
@pytest.mark.parametrize("role", [ROLE.EDITOR, ROLE.CLIENT, ROLE.VIEWER])
def test_other_roles_cannot_set_the_goal(client, brand, role):
    _member(client, brand, role)

    response = client.post(_url(brand, "workspaces:dashboard_goal"), {"monthly_post_goal": "20"})

    assert response.status_code == 403
    brand.refresh_from_db()
    assert brand.monthly_post_goal is None


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["0", "1000", "abc", "-3"])
def test_an_invalid_goal_is_refused_and_not_saved(client, brand, value):
    brand.monthly_post_goal = 12
    brand.save()
    _member(client, brand, ROLE.OWNER)

    response = client.post(_url(brand, "workspaces:dashboard_goal"), {"monthly_post_goal": value})

    assert json.loads(response.headers["HX-Trigger"])["showToast"]["tone"] == "error"
    brand.refresh_from_db()
    assert brand.monthly_post_goal == 12


@pytest.mark.django_db
def test_an_empty_goal_removes_it(client, brand):
    brand.monthly_post_goal = 12
    brand.save()
    _member(client, brand, ROLE.OWNER)

    client.post(_url(brand, "workspaces:dashboard_goal"), {"monthly_post_goal": ""})

    brand.refresh_from_db()
    assert brand.monthly_post_goal is None


@pytest.mark.django_db
def test_the_goal_of_another_brand_cannot_be_changed(client, brand, organization):
    other = Workspace.objects.create(organization=organization, name="Other")
    _member(client, other, ROLE.OWNER)

    response = client.post(_url(brand, "workspaces:dashboard_goal"), {"monthly_post_goal": "5"})

    assert response.status_code == 403
    brand.refresh_from_db()
    assert brand.monthly_post_goal is None


@pytest.mark.django_db
def test_the_month_counts_posts_once_and_only_this_months(brand, instagram):
    facebook = SocialAccount.objects.create(
        workspace=brand,
        platform="facebook",
        account_platform_id="fb-1",
        account_name="Brand FB",
        oauth_access_token="token",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    today = date(2026, 11, 17)
    noon = lambda d: datetime(2026, 11, d, 12, tzinfo=TZ)  # noqa: E731
    # One post on two networks counts once.
    both = _platform_post(instagram, status=PlatformPost.Status.PUBLISHED, published_at=noon(3))
    _platform_post(facebook, status=PlatformPost.Status.PUBLISHED, published_at=noon(3), post=both.post)
    # Published on one network, still scheduled on the other: counted as published only.
    half = _platform_post(instagram, status=PlatformPost.Status.PUBLISHED, published_at=noon(10))
    _platform_post(facebook, status=PlatformPost.Status.SCHEDULED, scheduled_at=noon(25), post=half.post)
    _platform_post(instagram, status=PlatformPost.Status.SCHEDULED, scheduled_at=noon(28))
    # Last month, next month, a draft and another brand's post stay out.
    _platform_post(instagram, status=PlatformPost.Status.PUBLISHED, published_at=datetime(2026, 10, 31, 12, tzinfo=TZ))
    _platform_post(instagram, status=PlatformPost.Status.SCHEDULED, scheduled_at=datetime(2026, 12, 1, 12, tzinfo=TZ))
    _platform_post(instagram, status=PlatformPost.Status.DRAFT, scheduled_at=noon(20))
    brand.monthly_post_goal = 10

    progress = month_goal_progress(brand, today, TZ)

    assert (progress["published"], progress["scheduled"], progress["remaining"]) == (2, 1, 7)
    assert progress["pace"] == "behind"
    assert progress["days_left"] == 13


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("goal", "published", "scheduled", "pace"),
    [(2, 2, 0, "done"), (3, 2, 1, "covered"), (4, 2, 0, "on_track"), (10, 2, 0, "behind"), (None, 2, 0, None)],
)
def test_the_month_pace(brand, instagram, goal, published, scheduled, pace):
    for day in range(1, published + 1):
        _platform_post(
            instagram, status=PlatformPost.Status.PUBLISHED, published_at=datetime(2026, 11, day, 12, tzinfo=TZ)
        )
    for day in range(25, 25 + scheduled):
        _platform_post(
            instagram, status=PlatformPost.Status.SCHEDULED, scheduled_at=datetime(2026, 11, day, 12, tzinfo=TZ)
        )
    brand.monthly_post_goal = goal

    assert month_goal_progress(brand, date(2026, 11, 10), TZ)["pace"] == pace


# --- what works best ------------------------------------------------------------


def _rated_post(account, *, published_at, fmt="default", reach=100, likes=5):
    post = _platform_post(account, status=PlatformPost.Status.PUBLISHED, published_at=published_at, fmt=fmt)
    for key, value in (("reach", reach), ("likes", likes)):
        PostInsightsSnapshot.objects.create(platform_post=post, metric_key=key, date=published_at.date(), value=value)
    return post


@pytest.mark.django_db
def test_formats_are_ranked_by_average_engagement(instagram):
    now = timezone.now()
    _rated_post(instagram, published_at=now - timedelta(days=1), fmt="reel", likes=10)
    _rated_post(instagram, published_at=now - timedelta(days=2), fmt="reel", likes=6)
    _rated_post(instagram, published_at=now - timedelta(days=3), likes=2)
    # No denominator yet: left out rather than counted as 0%.
    _platform_post(instagram, status=PlatformPost.Status.PUBLISHED, published_at=now - timedelta(days=1))
    # Outside the period.
    _rated_post(instagram, published_at=now - timedelta(days=40), likes=90)

    with translation.override("en"):
        result = post_performance(instagram, 30, TZ)

    assert [(row["label"], row["rate"], row["posts"], row["share"]) for row in result["formats"]] == [
        ("Reels", 8.0, 2, 100),
        ("Text", 2.0, 1, 25),
    ]
    assert result["rated_posts"] == 3
    assert result["best_day"] is None  # too few posts to tell


@pytest.mark.django_db
def test_the_best_day_and_time_need_enough_posts(instagram):
    base = timezone.localtime(timezone.now(), TZ).replace(hour=19, minute=0, second=0, microsecond=0)
    tuesday_evening = base - timedelta(days=(base.weekday() - 1) % 7 or 7)
    for week in range(3):
        _rated_post(instagram, published_at=tuesday_evening - timedelta(weeks=week), likes=9)
    morning = tuesday_evening + timedelta(days=2) - timedelta(hours=10)
    for week in range(3):
        _rated_post(instagram, published_at=morning - timedelta(weeks=week), likes=1)

    with translation.override("en"):
        result = post_performance(instagram, 30, TZ)

    with translation.override("en"):
        assert str(result["best_day"]) == "Tuesday"
    assert result["best_hours"] == (18, 21)


@pytest.mark.django_db
def test_the_results_pills_name_the_network_and_the_account_only_when_repeated(client, brand, instagram):
    AnalyticsPlatformConfig.objects.update_or_create(platform="instagram", defaults={"is_enabled": True})
    AnalyticsPlatformConfig.objects.update_or_create(platform="facebook", defaults={"is_enabled": True})
    for platform, name in (("facebook", "Brand FB"), ("instagram", "Second IG")):
        SocialAccount.objects.create(
            workspace=brand,
            platform=platform,
            account_platform_id=name,
            account_name=name,
            oauth_access_token="token",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
    _member(client, brand, ROLE.OWNER)

    with translation.override("en"):
        response = client.get(_url(brand))

    assert sorted(a.pill_label for a in response.context["rated_accounts"]) == [
        "Facebook",
        "Instagram · Brand IG",
        "Instagram · Second IG",
    ]


# --- my tasks, upcoming dates and the day's summary -------------------------------


def _task(brand, user, *, title, due=None, done=False):
    from apps.tasks.models import Task, TaskColumn

    column, _ = TaskColumn.objects.get_or_create(workspace=brand, name="Done" if done else "To do", is_done=done)
    task = Task.objects.create(workspace=brand, column=column, title=title, due_date=due)
    if user is not None:
        task.assignees.add(user)
    return task


@pytest.mark.django_db
def test_my_tasks_are_my_open_ones_most_urgent_first(brand, organization, user):
    from apps.workspaces.dashboard import my_open_tasks

    today = date(2026, 11, 17)
    other_brand = Workspace.objects.create(organization=organization, name="Other")
    _task(brand, user, title="later", due=date(2026, 11, 20))
    _task(brand, user, title="undated")
    _task(brand, user, title="today", due=today)
    _task(brand, user, title="late", due=date(2026, 11, 10))
    _task(brand, user, title="finished", due=date(2026, 11, 1), done=True)
    _task(brand, None, title="nobody's")
    _task(other_brand, user, title="other brand", due=today)

    result = my_open_tasks(brand, user, today)

    assert [(row["task"].title, row["due"]) for row in result["rows"]] == [
        ("late", "overdue"),
        ("today", "today"),
        ("later", "upcoming"),
        ("undated", None),
    ]
    assert result["total"] == 4


@pytest.mark.django_db
def test_a_client_gets_no_tasks_card_nor_tasks_in_the_summary(client, brand):
    member = _member(client, brand, ROLE.CLIENT)
    _task(brand, member, title="mine", due=timezone.localdate())

    response = client.get(_url(brand))

    assert response.context["my_tasks"] is None
    assert response.context["day"]["tasks"] is None


@pytest.mark.django_db
def test_the_summary_counts_only_what_the_person_can_act_on(brand, instagram, user):
    from apps.inbox.models import InboxMessage
    from apps.workspaces.dashboard import day_summary

    today = timezone.localdate(timezone=TZ)
    noon = timezone.make_aware(datetime.combine(today, datetime.min.time()), TZ) + timedelta(hours=12)
    _platform_post(instagram, status=PlatformPost.Status.SCHEDULED, scheduled_at=noon)
    _platform_post(instagram, status=PlatformPost.Status.PENDING_REVIEW, scheduled_at=noon + timedelta(days=3))
    _platform_post(instagram, status=PlatformPost.Status.SCHEDULED, scheduled_at=noon + timedelta(days=1))
    _task(brand, user, title="due", due=today)
    InboxMessage.objects.create(
        workspace=brand,
        social_account=instagram,
        platform_message_id="m1",
        sender_name="Ana",
        sender_handle="ana",
        body="Oi",
        status=InboxMessage.Status.UNREAD,
        received_at=timezone.now(),
    )

    full = day_summary(brand, user, {"approve_posts": True, "use_inbox": True}, today, TZ, is_team=True)
    limited = day_summary(brand, user, {}, today, TZ, is_team=False)

    assert (full["posts"], full["approvals"], full["messages"], full["tasks"], full["total"]) == (1, 1, 1, 1, 4)
    assert (limited["approvals"], limited["messages"], limited["tasks"], limited["total"]) == (None, None, None, 1)


@pytest.mark.django_db
def test_upcoming_dates_say_whether_the_day_already_has_its_post(brand, instagram, monkeypatch):
    from apps.workspaces import dashboard

    first, second = date(2026, 11, 20), date(2026, 11, 27)
    monkeypatch.setattr(
        "apps.calendar.services.upcoming_dates",
        lambda workspace, start, days: [
            {"date": first, "days": 3, "name": "A", "emoji": "", "event": None},
            {"date": second, "days": 10, "name": "B", "emoji": "", "event": None},
        ],
    )
    monkeypatch.setattr("apps.calendar.services.special_dates_enabled", lambda workspace: True)
    # 23:30 in São Paulo on the 20th is already the 21st in UTC: still the 20th's post.
    _platform_post(
        instagram, status=PlatformPost.Status.SCHEDULED, scheduled_at=datetime(2026, 11, 20, 23, 30, tzinfo=TZ)
    )
    _platform_post(instagram, status=PlatformPost.Status.DRAFT, scheduled_at=datetime(2026, 11, 27, 12, tzinfo=TZ))

    entries = dashboard.upcoming_dates_with_posts(brand, date(2026, 11, 17), TZ)

    assert [(e["name"], e["has_post"]) for e in entries] == [("A", True), ("B", False)]


@pytest.mark.django_db
def test_upcoming_dates_list_special_dates_even_when_the_calendar_hides_them(brand, monkeypatch):
    """The calendar's switch hides special dates from its grid only: the card
    puts them among the brand's own events, by date."""
    from apps.workspaces import dashboard

    today = date(2026, 11, 18)
    event_day = date(2026, 11, 19)
    monkeypatch.setattr(
        "apps.calendar.services.upcoming_dates",
        lambda workspace, start, days: [{"date": event_day, "days": 1, "name": "Launch", "emoji": "", "event": None}],
    )
    monkeypatch.setattr("apps.calendar.services.special_dates_enabled", lambda workspace: False)

    entries = dashboard.upcoming_dates_with_posts(brand, today, TZ)

    names = [e["name"] for e in entries]
    assert names[0] == "Launch"
    assert "Black Friday" in names
    assert [e["date"] for e in entries] == sorted(e["date"] for e in entries)
    assert all(e["days"] == (e["date"] - today).days for e in entries)


@pytest.mark.django_db
def test_upcoming_dates_fall_back_to_the_next_special_dates_when_none_is_near(brand, monkeypatch):
    """Nothing in the next 90 days: the card still lists the brand's next three
    special dates of the year, with their distance."""
    from apps.workspaces import dashboard

    monkeypatch.setattr("apps.calendar.services.upcoming_dates", lambda workspace, start, days: [])
    monkeypatch.setattr("apps.calendar.services.special_dates_enabled", lambda workspace: True)
    today = date(2026, 11, 18)

    entries = dashboard.upcoming_dates_with_posts(brand, today, TZ)

    assert len(entries) == 3
    assert [e["date"] for e in entries] == sorted(e["date"] for e in entries)
    assert all(e["date"] >= today and e["days"] == (e["date"] - today).days for e in entries)
    assert all(e["event"] is None and e["has_post"] is False for e in entries)


# --- period -------------------------------------------------------------------


@pytest.mark.django_db
def test_a_custom_period_counts_only_the_posts_of_those_days(instagram):
    end = timezone.now().date() - timedelta(days=20)
    inside = datetime.combine(end - timedelta(days=2), datetime.min.time(), tzinfo=TZ).replace(hour=12)
    _rated_post(instagram, published_at=inside, fmt="reel", likes=10)
    _rated_post(instagram, published_at=inside + timedelta(days=10), likes=2)  # after the end

    with translation.override("en"):
        result = post_performance(instagram, 7, TZ, end)

    assert [(row["label"], row["posts"]) for row in result["formats"]] == [("Reels", 1)]


@pytest.mark.django_db
def test_the_dashboard_keeps_the_picked_dates_on_its_links(client, brand, instagram):
    AnalyticsPlatformConfig.objects.update_or_create(platform="instagram", defaults={"is_enabled": True})
    _member(client, brand, ROLE.VIEWER)
    end = timezone.localdate(timezone=TZ) - timedelta(days=3)
    start = end - timedelta(days=9)

    page = client.get(_url(brand), {"start": start.isoformat(), "end": end.isoformat()})
    networks = client.get(
        _url(brand), {"start": start.isoformat(), "end": end.isoformat(), "partial": "networks"}, HTTP_HX_REQUEST="true"
    )

    period = page.context["period"]
    assert (period.custom, period.days, period.end) == (True, 10, end)
    body = page.content.decode()
    assert f"start={start.isoformat()}&amp;end={end.isoformat()}" in body
    assert "range=30" in body  # the presets are still offered
    assert networks.status_code == 200


def test_see_more_opens_the_special_dates_page_with_the_date_picked():
    """Every date leads to the special-dates page: a special date picked there,
    one of the brand's own events (not in that page's list) to the page as it is.
    Without ``create_posts`` that page can't be opened, so no link."""
    from types import SimpleNamespace

    from apps.workspaces.views import _date_links

    special = {"date": date(2026, 11, 27), "key": "b:black-friday", "event": None}
    event = {"date": date(2026, 12, 2), "event": SimpleNamespace(pk=1)}
    brand_id = "00000000-0000-0000-0000-000000000001"
    dates_url = reverse("composer:create_dates", kwargs={"workspace_id": brand_id})

    _date_links([special, event], brand_id, can_plan=True)
    assert special["more_url"] == dates_url + "?date=b%3Ablack-friday&day=2026-11-27"
    assert event["more_url"] == dates_url

    _date_links([special, event], brand_id, can_plan=False)
    assert special["more_url"] == event["more_url"] == ""
