"""Sending to the task board from other pages ("Send as task"): the board opens its new
task window filled from an idea, a template, a special date, a calendar day or a handoff."""

from __future__ import annotations

import html
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


def _board(client, workspace, **params):
    return client.get(reverse("tasks:board", kwargs={"workspace_id": workspace.id}), {"new": "1", **params})


def _initial(response):
    """The new task window's data-initial (what its script opens with)."""
    raw = response.content.decode().split('data-initial="', 1)[1].split('"', 1)[0]
    return json.loads(html.unescape(raw) or "{}")


def test_an_idea_opens_the_boards_window_filled_to_ask_for_its_arts(workspace, owner):
    target = timezone.localdate() + timedelta(days=20)
    idea = Idea.objects.create(
        workspace=workspace, title="Launch", description="Launch caption", target_date=target, tags=["launch"]
    )

    initial = _initial(_board(_client(owner), workspace, idea=idea.id))

    fields = initial["fields"]
    assert fields["title"] == "Launch"
    # The idea's text is the post's caption, never the briefing.
    assert fields["post_caption"] == "Launch caption" and fields["briefing"] == ""
    # Due the brand's lead days before the content date.
    assert fields["due_date"] == (target - timedelta(days=3)).isoformat()
    assert initial["wantsArt"] and initial["wantsPost"] and initial["tags"] == ["launch"]
    assert initial["hidden"] == {"idea": str(idea.id)} and "Launch" in initial["note"]


def test_the_board_alone_opens_no_window(workspace, owner):
    response = _client(owner).get(reverse("tasks:board", kwargs={"workspace_id": workspace.id}))

    assert _initial(response) == {}


def test_an_idea_of_another_workspace_cannot_be_opened_or_linked(workspace, owner):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    idea = Idea.objects.create(workspace=other, title="Theirs")
    client = _client(owner)

    assert _board(client, workspace, idea=idea.id).status_code == 404
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

    initial = _initial(_board(_client(owner), workspace, idea=idea.id))

    assert initial["assignees"] == [str(designer.pk)]


def test_a_special_date_opens_the_window_with_its_briefing_and_arts(workspace, owner):
    from apps.calendar.models import SpecialDate

    day = timezone.localdate() + timedelta(days=30)
    row = SpecialDate.objects.create(
        name="Pizza Day",
        month=day.month,
        day=day.day,
        description="Celebrate pizza",
        tags="pizza, food",
        briefing="Melted cheese close-ups",
        art_items=[{"format": "ig_reel", "count": 1, "note": "Slicing the pizza"}],
    )

    initial = _initial(_board(_client(owner), workspace, date=f"d:{row.id}", day=day.isoformat()))

    assert initial["fields"]["title"] == "Pizza Day"
    assert initial["fields"]["briefing"] == "Melted cheese close-ups"
    assert initial["fields"]["due_date"] == (day - timedelta(days=3)).isoformat()
    assert initial["artItems"] == [{"format": "ig_reel", "count": 1, "note": "Slicing the pizza"}]
    assert initial["tags"] == ["pizza", "food"]


def test_a_built_in_date_without_a_briefing_takes_its_description(workspace, owner):
    from datetime import date

    from apps.calendar.holidays import BUILT_IN_ARTS

    day = date(timezone.localdate().year + 1, 12, 25)
    initial = _initial(_board(_client(owner), workspace, date="b:christmas", day=day.isoformat()))

    assert initial["fields"]["title"] == "Christmas"
    if not BUILT_IN_ARTS.get("christmas"):
        assert initial["fields"]["briefing"]


def test_a_date_the_brand_does_not_see_that_day_is_not_found(workspace, owner):
    from apps.calendar.models import DateSegment, SpecialDate

    segment = DateSegment.objects.create(name="Bakeries")
    day = timezone.localdate() + timedelta(days=10)
    row = SpecialDate.objects.create(segment=segment, name="Bread Day", month=day.month, day=day.day)
    client = _client(owner)

    assert _board(client, workspace, date=f"d:{row.id}", day=day.isoformat()).status_code == 404
    assert _board(client, workspace, date="b:christmas", day=day.isoformat()).status_code == 404


def test_a_calendar_day_opens_a_plain_task_due_that_day(workspace, owner):
    day = timezone.localdate() + timedelta(days=5)

    initial = _initial(_board(_client(owner), workspace, day=day.isoformat()))

    assert not initial["wantsArt"] and not initial["wantsPost"]
    assert initial["fields"]["due_date"] == day.isoformat()


def test_a_handoff_opens_once_and_only_on_its_workspace(workspace, owner):
    from django.test import RequestFactory

    from apps.tasks.views import send_to_board

    client = _client(owner)
    request = RequestFactory().post("/")
    request.workspace = workspace
    request.session = client.session
    response = send_to_board(
        request, {"title": "Post offer", "briefing": "Brief"}, note="From X", submit_to="/x/", hidden={"item_id": 7}
    )
    request.session.save()

    assert response["HX-Redirect"].endswith("?new=handoff")
    initial = _initial(_board(client, workspace, new="handoff"))
    assert initial["fields"]["title"] == "Post offer" and initial["fields"]["briefing"] == "Brief"
    assert (initial["submitTo"], initial["hidden"], initial["note"]) == ("/x/", {"item_id": "7"}, "From X")
    # Taken: a reload is the board alone.
    assert _initial(_board(client, workspace, new="handoff")) == {}


def test_a_client_or_viewer_cannot_open_the_board_window(workspace):
    client_user = _user("client@example.com", workspace, Role.CLIENT)
    viewer = _user("viewer@example.com", workspace, Role.VIEWER)
    idea = Idea.objects.create(workspace=workspace, title="Launch")

    assert _board(_client(client_user), workspace, idea=idea.id).status_code == 403
    assert "newItemForm(" not in _board(_client(viewer), workspace, idea=idea.id).content.decode()


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

    body = _board(_client(owner), workspace).content.decode()

    assert "All formats of TikTok" in body and "All formats of Pinterest" not in body
    assert body.count("All formats of Instagram") == 1
    instagram_row = body.split("All formats of Instagram")[0].rsplit('class="art-channel"', 1)[1]
    assert "Insta" in instagram_row and "Not connected" not in instagram_row


def test_the_pages_send_to_the_boards_window(workspace, owner):
    client = _client(owner)
    board = reverse("tasks:board", kwargs={"workspace_id": workspace.id})
    ws = {"workspace_id": workspace.id}
    expected = {
        reverse("composer:create_landing", kwargs=ws): f"{board}?new=1&idea=",
        reverse("composer:create_templates", kwargs=ws): f'data-task-url="{board}"',
        reverse("composer:create_dates", kwargs=ws): f'data-task-url="{board}"',
        reverse("calendar:calendar", kwargs=ws) + "?mode=calendar": f"{board}?new=1&day=",
    }
    for url, link in expected.items():
        assert link in client.get(url).content.decode(), url


# The arts a content template asks for, carried to the idea and the task.

ART_ITEMS = [
    {"format": "ig_feed_portrait", "count": 3, "note": "One step per slide"},
    {"format": "ig_story", "count": 1, "note": ""},
]


def test_art_items_keep_known_formats_once_with_a_bounded_count_and_note():
    from apps.tasks.creative_formats import clean_art_items

    cleaned = clean_art_items(
        [
            {"format": "ig_story", "count": "99", "note": "x" * 300},
            {"format": "ig_story", "count": 2},
            {"format": "nope", "count": 1},
            {"format": "ig_feed_square", "count": "abc"},
            "junk",
        ]
    )

    assert cleaned == [
        {"format": "ig_story", "count": 20, "note": "x" * 200},
        {"format": "ig_feed_square", "count": 1, "note": ""},
    ]
    assert clean_art_items("not a list") == []


def test_the_template_form_saves_its_briefing_and_arts():
    from apps.composer.forms import IdeaTemplateForm

    form = IdeaTemplateForm(
        data={
            "title": "Step by step",
            "category": "how-to",
            "briefing": "Clean, bold numbers",
            "art_items": json.dumps([*ART_ITEMS, {"format": "nope"}]),
        }
    )

    assert form.is_valid(), form.errors
    template = form.save()
    assert template.briefing == "Clean, bold numbers" and template.art_items == ART_ITEMS


def test_a_template_becomes_an_idea_with_its_arts_and_the_idea_a_task_with_them(workspace, owner):
    from apps.composer.models import IdeaTemplate

    template = IdeaTemplate.objects.create(title="Step by step", body="Text", briefing="Bold", art_items=ART_ITEMS)
    client = _client(owner)

    client.post(
        reverse("composer:idea_from_template", kwargs={"workspace_id": workspace.id, "template_id": template.id})
    )

    idea = Idea.objects.get(workspace=workspace, title="Step by step")
    assert (idea.briefing, idea.art_items) == ("Bold", ART_ITEMS)
    initial = _initial(_board(client, workspace, idea=idea.id))
    assert initial["fields"]["briefing"] == "Bold" and initial["artItems"] == ART_ITEMS


def test_a_template_sent_as_a_task_opens_with_its_arts_ticked(workspace, owner):
    from apps.composer.models import IdeaTemplate

    template = IdeaTemplate.objects.create(title="Step by step", body="Text", briefing="Bold", art_items=ART_ITEMS)

    initial = _initial(_board(_client(owner), workspace, template=template.id))

    assert initial["wantsArt"] and initial["fields"]["briefing"] == "Bold"
    assert initial["artItems"] == ART_ITEMS


def test_each_art_becomes_as_many_items_as_asked_with_its_note(workspace, owner):
    response = _client(owner).post(
        reverse("tasks:task_create", kwargs={"workspace_id": workspace.id}),
        {
            "title": "Step by step",
            "request_art": "1",
            "formats": ["ig_feed_portrait", "ig_story"],
            "art_items": json.dumps(ART_ITEMS),
        },
        HTTP_HX_REQUEST="true",
    )

    assert "taskCreated" in response["HX-Trigger"]
    items = list(Task.objects.get().checklist.order_by("position").values_list("text", "format_key", "width"))
    assert items == [
        ("Portrait post 1/3 — One step per slide", "ig_feed_portrait", 1080),
        ("Portrait post 2/3 — One step per slide", "ig_feed_portrait", 1080),
        ("Portrait post 3/3 — One step per slide", "ig_feed_portrait", 1080),
        ("Story", "ig_story", 1080),
    ]
