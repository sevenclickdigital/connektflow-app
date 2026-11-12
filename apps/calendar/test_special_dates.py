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


def test_an_idea_keeps_its_date_and_only_its_own_workspaces_event(workspace, owner_client):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    foreign = _event(other, date(2026, 5, 10))
    url = reverse("composer:idea_create", kwargs={"workspace_id": workspace.id})

    owner_client.post(url, {"title": "Gift guide", "target_date": "2026-05-10", "calendar_event": str(foreign.id)})

    idea = Idea.objects.get(title="Gift guide")
    assert idea.target_date == date(2026, 5, 10)
    assert idea.calendar_event is None
