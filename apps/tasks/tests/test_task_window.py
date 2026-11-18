"""The board's new task window opened from other pages ("Send as task"): filled from an
idea, a template, a special date or a calendar day, and saved like the board's."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import Idea
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.tasks import services
from apps.tasks.models import Task
from apps.workspaces.models import Workspace

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_english")]

Role = WorkspaceMembership.WorkspaceRole


def _user(email, workspace, role):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.get_or_create(
        user=user, organization=workspace.organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
    )
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user


@pytest.fixture
def _english(settings):
    settings.LANGUAGE_CODE = "en-us"


@pytest.fixture
def workspace():
    org = Organization.objects.create(name="Acme")
    return Workspace.objects.create(organization=org, name="Acme Social")


@pytest.fixture
def owner(workspace):
    return _user("owner@example.com", workspace, Role.OWNER)


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def _window(client, workspace, **params):
    return client.get(reverse("tasks:task_window", kwargs={"workspace_id": workspace.id}), params)


def _initial(body):
    """The window's data-initial (what its script starts from)."""
    raw = body.split('data-initial="', 1)[1].split('"', 1)[0]
    return json.loads(raw.replace("&quot;", '"'))


def _between(body, start, end="</textarea>"):
    return body.split(start, 1)[1].split(end, 1)[0]


def test_an_idea_opens_the_window_filled_to_ask_for_its_arts(workspace, owner):
    target = timezone.localdate() + timedelta(days=20)
    idea = Idea.objects.create(
        workspace=workspace, title="Launch", description="Launch caption", target_date=target, tags=["launch"]
    )

    response = _window(_client(owner), workspace, idea=idea.id)

    body = response.content.decode()
    assert response.status_code == 200 and "<html" not in body
    assert "data-standalone" in body and 'value="Launch"' in body
    assert f'name="idea" value="{idea.id}"' in body
    # The idea's text is the post's caption, never the briefing.
    assert "Launch caption" in _between(body, 'name="post_caption"')
    assert "Launch caption" not in _between(body, 'name="briefing"')
    # Due the brand's lead days before the content date.
    assert (target - timedelta(days=3)).isoformat() in body
    initial = _initial(body)
    assert initial["wantsArt"] and initial["wantsPost"] and initial["tags"] == ["launch"]


def test_an_idea_of_another_workspace_cannot_be_opened_or_linked(workspace, owner):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    idea = Idea.objects.create(workspace=other, title="Theirs")
    client = _client(owner)

    assert _window(client, workspace, idea=idea.id).status_code == 404
    response = client.post(
        reverse("tasks:task_create", kwargs={"workspace_id": workspace.id}),
        {"title": "Mine", "idea": idea.id},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 404 and not Task.objects.exists()


def test_the_task_sent_from_an_idea_is_linked_to_it(workspace, owner):
    idea = Idea.objects.create(workspace=workspace, title="Launch")

    response = _client(owner).post(
        reverse("tasks:task_create", kwargs={"workspace_id": workspace.id}),
        {"title": "Launch", "idea": idea.id, "request_art": "1", "formats": ["ig_story"]},
        HTTP_HX_REQUEST="true",
    )

    assert "taskCreated" in response["HX-Trigger"]
    assert Task.objects.get().idea == idea


def test_the_window_preselects_the_default_art_assignee(workspace, owner):
    designer = _user("designer@example.com", workspace, Role.CONTRIBUTOR)
    settings_row = services.art_settings(workspace)
    settings_row.art_assignee = designer
    settings_row.save()
    idea = Idea.objects.create(workspace=workspace, title="Launch")

    initial = _initial(_window(_client(owner), workspace, idea=idea.id).content.decode())

    assert initial["assignees"] == [str(designer.pk)]


def test_a_special_date_opens_the_window_with_its_theme_as_the_briefing(workspace, owner):
    day = timezone.localdate() + timedelta(days=30)

    body = _window(
        _client(owner), workspace, title="Mother's Day", day=day.isoformat(), brief="Hug", tags=["mom"]
    ).content.decode()

    assert "Hug" in _between(body, 'name="briefing"')
    assert (day - timedelta(days=3)).isoformat() in body
    assert _initial(body)["tags"] == ["mom"]


def test_a_calendar_day_opens_a_plain_task_due_that_day(workspace, owner):
    day = timezone.localdate() + timedelta(days=5)

    body = _window(_client(owner), workspace, day=day.isoformat()).content.decode()

    initial = _initial(body)
    assert not initial["wantsArt"] and not initial["wantsPost"]
    assert f'value="{day.isoformat()}"' in body


def test_the_window_lists_the_networks_on_offer_and_hides_the_hidden_ones(workspace, owner):
    from apps.social_accounts.models import PlatformVisibility

    SocialAccount.objects.create(
        workspace=workspace,
        platform="instagram",
        account_platform_id="ig-1",
        account_name="Insta",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    PlatformVisibility.objects.update_or_create(platform="pinterest", defaults={"is_visible": False})

    body = _window(_client(owner), workspace).content.decode()

    assert "All formats of TikTok" in body and "Pinterest" not in body
    assert body.count("All formats of Instagram") == 1
    instagram_row = body.split("All formats of Instagram")[0].rsplit('class="art-channel"', 1)[1]
    assert "Insta" in instagram_row and "Not connected" not in instagram_row


def test_a_client_or_viewer_cannot_open_the_window(workspace):
    client_user = _user("client@example.com", workspace, Role.CLIENT)
    viewer = _user("viewer@example.com", workspace, Role.VIEWER)

    assert _window(_client(client_user), workspace).status_code == 403
    assert _window(_client(viewer), workspace).status_code == 403


def test_the_pages_that_send_to_the_board_host_the_window(workspace, owner):
    client = _client(owner)
    pages = [
        reverse("composer:create_landing", kwargs={"workspace_id": workspace.id}),
        reverse("composer:create_templates", kwargs={"workspace_id": workspace.id}),
        reverse("composer:create_dates", kwargs={"workspace_id": workspace.id}),
        reverse("calendar:calendar", kwargs={"workspace_id": workspace.id}) + "?mode=calendar",
    ]
    for url in pages:
        body = client.get(url).content.decode()
        assert 'x-data="taskWindowHost(' in body, url
        assert "js/task-window.js" in body, url


def test_a_viewer_gets_no_window_host(workspace):
    viewer = _user("viewer@example.com", workspace, Role.VIEWER)

    body = _client(viewer).get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})).content.decode()

    assert "taskWindowHost(" not in body
