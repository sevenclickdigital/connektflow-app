"""Brazilian special dates, repeating events and the idea board's upcoming dates."""

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.calendar.holidays import easter, special_dates_between
from apps.calendar.models import CustomCalendarEvent
from apps.calendar.services import events_by_date, special_dates_enabled, upcoming_dates
from apps.composer.models import Idea
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace():
    org = Organization.objects.create(name="Acme")
    return Workspace.objects.create(organization=org, name="Acme Social")


@pytest.fixture
def owner_client(workspace):
    user = User.objects.create_user(email="o@example.com", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Carnival", date(2026, 2, 17)),
        ("Easter", date(2026, 4, 5)),
        ("Mother's Day", date(2026, 5, 10)),
        ("Father's Day", date(2026, 8, 9)),
        ("Corpus Christi", date(2026, 6, 4)),
        ("Black Friday", date(2026, 11, 27)),
        ("Valentine's Day", date(2026, 6, 12)),
    ],
)
def test_movable_and_fixed_dates_of_2026(settings, name, expected):
    settings.LANGUAGE_CODE = "en-us"
    found = {d["name"]: d["date"] for d in special_dates_between(date(2026, 1, 1), date(2026, 12, 31))}

    assert found[name] == expected


def test_easter_across_years():
    assert [easter(y) for y in (2024, 2025, 2027)] == [date(2024, 3, 31), date(2025, 4, 20), date(2027, 3, 28)]


def _event(workspace, start, end=None, recurrence="none", title="Event"):
    return CustomCalendarEvent.objects.create(
        workspace=workspace, title=title, start_date=start, end_date=end or start, recurrence=recurrence
    )


def test_a_monthly_event_on_the_31st_falls_on_the_last_day_of_short_months(workspace):
    _event(workspace, date(2026, 1, 31), recurrence="monthly", title="Payday")

    days = events_by_date(workspace, date(2026, 2, 1), date(2026, 4, 30))

    assert sorted(days) == [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


def test_a_yearly_event_repeats_and_keeps_its_length(workspace):
    _event(workspace, date(2024, 3, 14), date(2024, 3, 16), recurrence="yearly", title="Anniversary week")

    days = events_by_date(workspace, date(2026, 3, 1), date(2026, 3, 31))

    assert sorted(days) == [date(2026, 3, 14), date(2026, 3, 15), date(2026, 3, 16)]
    assert days[date(2026, 3, 15)][0].start_date == date(2026, 3, 14)


def test_a_one_off_event_does_not_repeat(workspace):
    _event(workspace, date(2026, 1, 10))

    assert events_by_date(workspace, date(2026, 2, 1), date(2026, 2, 28)) == {}


def test_events_of_other_workspaces_are_not_listed(workspace):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    _event(other, date(2026, 2, 10), recurrence="monthly")

    assert events_by_date(workspace, date(2026, 2, 1), date(2026, 2, 28)) == {}


def test_upcoming_dates_mix_special_dates_and_events(workspace, settings):
    settings.LANGUAGE_CODE = "en-us"
    _event(workspace, date(2026, 5, 20), title="Store anniversary")

    names = [(e["name"], e["days"]) for e in upcoming_dates(workspace, date(2026, 5, 1), days=30)]

    assert names == [("Labour Day", 0), ("Mother's Day", 9), ("Store anniversary", 19)]


def test_the_workspace_can_hide_the_special_dates(workspace, owner_client):
    assert special_dates_enabled(workspace)

    owner_client.post(reverse("calendar:special_dates_toggle", kwargs={"workspace_id": workspace.id}))

    assert not special_dates_enabled(workspace)
    assert all(e["event"] for e in upcoming_dates(workspace, date(2026, 5, 1)))


def test_the_workspace_can_hide_its_own_events_from_the_calendar(workspace, owner_client):
    from apps.calendar.services import custom_events_enabled
    from apps.social_accounts.models import SocialAccount

    # The grid is only built for a workspace with a connected channel.
    SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig",
        account_name="ig",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    _event(workspace, date(2026, 11, 20), title="Campaign start")
    url = reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
    month = {"mode": "calendar", "view": "month", "date": "2026-11-01"}

    def shown(response):
        return [e.title for week in response.context["calendar_weeks"] for day in week for e in day["events"]]

    assert custom_events_enabled(workspace)
    assert "Campaign start" in shown(owner_client.get(url, month))

    owner_client.post(reverse("calendar:custom_events_toggle", kwargs={"workspace_id": workspace.id}))

    assert not custom_events_enabled(workspace)
    response = owner_client.get(url, month)
    assert shown(response) == []
    holidays_only = response.context["month_summary"]["special_dates"]
    owner_client.post(reverse("calendar:special_dates_toggle", kwargs={"workspace_id": workspace.id}))
    assert holidays_only > 0
    assert owner_client.get(url, month).context["month_summary"]["special_dates"] == 0


def test_an_idea_keeps_its_date_and_only_its_own_workspaces_event(workspace, owner_client):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    foreign = _event(other, date(2026, 5, 10))
    url = reverse("composer:idea_create", kwargs={"workspace_id": workspace.id})

    owner_client.post(url, {"title": "Gift guide", "target_date": "2026-05-10", "calendar_event": str(foreign.id)})

    idea = Idea.objects.get(title="Gift guide")
    assert idea.target_date == date(2026, 5, 10)
    assert idea.calendar_event is None


def test_the_add_date_modal_gets_a_toast_and_closes_only_once_saved(workspace, owner_client):
    import json

    url = reverse("calendar:event_create", kwargs={"workspace_id": workspace.id})
    data = {"title": "", "start_date": "2026-11-20", "end_date": "2026-11-20", "color": "#7C3AED"}

    refused = owner_client.post(url, data, HTTP_HX_REQUEST="true")
    refused_events = json.loads(refused["HX-Trigger"])
    assert refused_events["showToast"]["tone"] == "error"
    assert "event-saved" not in refused_events
    assert not CustomCalendarEvent.objects.filter(workspace=workspace).exists()

    saved = owner_client.post(url, {**data, "title": "Campaign start"}, HTTP_HX_REQUEST="true")
    saved_events = json.loads(saved["HX-Trigger"])
    assert saved_events["showToast"]["tone"] == "success"
    assert saved_events["event-saved"] and saved_events["calendarRefresh"]
    assert CustomCalendarEvent.objects.get(workspace=workspace).title == "Campaign start"


def test_a_campaign_is_edited_and_deleted_from_its_modal(workspace, owner_client):
    import json

    campaign = _event(workspace, date(2026, 11, 20), title="Campaign start")
    kwargs = {"workspace_id": workspace.id, "event_id": campaign.id}
    data = {"title": "Black Friday", "start_date": "2026-11-27", "end_date": "2026-11-29", "color": "#EF4444"}

    edited = owner_client.post(reverse("calendar:event_edit", kwargs=kwargs), data, HTTP_HX_REQUEST="true")
    assert json.loads(edited["HX-Trigger"])["event-saved"]
    campaign.refresh_from_db()
    assert (campaign.title, campaign.start_date, campaign.end_date, campaign.color) == (
        "Black Friday",
        date(2026, 11, 27),
        date(2026, 11, 29),
        "#EF4444",
    )

    deleted = owner_client.post(reverse("calendar:event_delete", kwargs=kwargs), HTTP_HX_REQUEST="true")
    assert json.loads(deleted["HX-Trigger"])["calendarRefresh"]
    assert not CustomCalendarEvent.objects.filter(id=campaign.id).exists()


def test_another_workspaces_campaign_cannot_be_edited_or_deleted(workspace, owner_client):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    foreign = _event(other, date(2026, 11, 20), title="Theirs")
    kwargs = {"workspace_id": workspace.id, "event_id": foreign.id}

    assert owner_client.post(reverse("calendar:event_edit", kwargs=kwargs), {"title": "Mine"}).status_code == 404
    assert owner_client.post(reverse("calendar:event_delete", kwargs=kwargs)).status_code == 404
    foreign.refresh_from_db()
    assert foreign.title == "Theirs"


def test_the_drafts_button_hides_drafts_from_the_calendar(workspace, owner_client):
    from datetime import datetime

    from apps.calendar.services import drafts_shown
    from apps.composer.models import PlatformPost, Post
    from apps.social_accounts.models import SocialAccount

    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig",
        account_name="ig",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    when = timezone.make_aware(datetime(2026, 11, 20, 12, 0))
    draft = PlatformPost.objects.create(
        post=Post.objects.create(workspace=workspace, caption="draft"),
        social_account=account,
        status="draft",
        scheduled_at=when,
    )
    scheduled = PlatformPost.objects.create(
        post=Post.objects.create(workspace=workspace, caption="scheduled"),
        social_account=account,
        status="scheduled",
        scheduled_at=when,
    )
    url = reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
    month = {"mode": "calendar", "view": "month", "date": "2026-11-01"}

    def shown():
        weeks = owner_client.get(url, month).context["calendar_weeks"]
        return {pp.id for week in weeks for day in week for pp in day["posts"]}

    assert drafts_shown(workspace)
    assert shown() == {draft.id, scheduled.id}

    owner_client.post(reverse("calendar:drafts_toggle", kwargs={"workspace_id": workspace.id}))

    assert not drafts_shown(workspace)
    assert shown() == {scheduled.id}


def test_a_draft_with_no_date_shows_on_the_day_it_was_created(workspace, owner_client):
    from apps.composer.models import PlatformPost, Post
    from apps.social_accounts.models import SocialAccount

    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig",
        account_name="ig",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    draft = PlatformPost.objects.create(
        post=Post.objects.create(workspace=workspace, caption="no date"), social_account=account, status="draft"
    )
    created = draft.post.created_at.date()
    url = reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})

    weeks = owner_client.get(url, {"mode": "calendar", "view": "month", "date": created.isoformat()}).context[
        "calendar_weeks"
    ]

    day = next(d for week in weeks for d in week if d["date"] == created)
    assert draft.id in [pp.id for pp in day["posts"]]
