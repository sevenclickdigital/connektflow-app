"""Art requests: from an idea to a task per format, the delivered files, and the post they make."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User
from apps.composer.models import Idea, PlatformPost
from apps.media_library.services import _fk_referenced_asset_ids
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.notifications.models import EventType, Notification
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.tasks import services
from apps.tasks.models import Task
from apps.workspaces.models import Workspace

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_english", "_media_root")]

Role = WorkspaceMembership.WorkspaceRole


@pytest.fixture
def _english(settings):
    settings.LANGUAGE_CODE = "en-us"


@pytest.fixture
def _media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


def _user(email, workspace, role):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.get_or_create(
        user=user, organization=workspace.organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
    )
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user


@pytest.fixture
def workspace():
    org = Organization.objects.create(name="Acme")
    return Workspace.objects.create(organization=org, name="Acme Social")


@pytest.fixture
def owner(workspace):
    return _user("owner@example.com", workspace, Role.OWNER)


@pytest.fixture
def designer(workspace):
    return _user("designer@example.com", workspace, Role.CONTRIBUTOR)


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def _png(width, height, name="art.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (240, 120, 40)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def _connect(workspace, platform, name):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-1",
        account_name=name,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _request_url(workspace):
    return reverse("tasks:request_arts", kwargs={"workspace_id": workspace.id})


def _deliver(client, task, item, upload):
    return client.post(
        reverse(
            "tasks:item_deliver",
            kwargs={"workspace_id": task.workspace_id, "task_id": task.id, "item_id": item.id},
        ),
        {"file": upload},
    )


def test_requesting_arts_makes_one_task_with_an_item_per_format(workspace, owner, designer):
    idea = Idea.objects.create(workspace=workspace, title="Mother's Day", target_date=date(2026, 5, 10))

    response = _client(owner).post(
        _request_url(workspace),
        {
            "idea": idea.id,
            "title": "Arts: Mother's Day",
            "formats": ["ig_story", "ig_feed_portrait", "not_a_format"],
            "assignees": [str(designer.pk)],
        },
    )

    task = Task.objects.get()
    assert response.status_code == 302 and str(task.pk) in response["Location"]
    assert task.idea == idea
    assert list(task.assignees.all()) == [designer]
    # Catalogue order, not click order; unknown keys dropped.
    assert [(i.text, i.width, i.height) for i in task.checklist.all()] == [
        ("Portrait post", 1080, 1350),
        ("Story", 1080, 1920),
    ]


def test_the_form_dates_the_arts_before_the_content(workspace, owner):
    target = timezone.localdate() + timedelta(days=20)
    idea = Idea.objects.create(workspace=workspace, title="Launch", target_date=target)

    body = _client(owner).get(_request_url(workspace), {"idea": idea.id}).content.decode()

    assert (target - timedelta(days=3)).isoformat() in body
    assert "Arts: Launch" in body


def test_a_request_without_formats_is_refused(workspace, owner):
    response = _client(owner).post(_request_url(workspace), {"title": "Nothing", "formats": []})

    assert response.status_code == 400
    assert not Task.objects.exists()


def test_the_default_art_assignee_is_used_when_nobody_is_picked(workspace, owner, designer):
    settings_row = services.art_settings(workspace)
    settings_row.art_assignee = designer
    settings_row.save()

    task = services.request_arts(workspace, owner, title="Arts", format_keys=["fb_cover"])

    assert list(task.assignees.all()) == [designer]


def test_an_idea_of_another_workspace_cannot_be_used(workspace, owner):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    idea = Idea.objects.create(workspace=other, title="Theirs")

    assert _client(owner).get(_request_url(workspace), {"idea": idea.id}).status_code == 404


def test_delivering_every_art_sends_the_task_to_review_and_tells_its_author(workspace, owner, designer):
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_story"], assignees=[designer])
    item = task.checklist.get()

    response = _deliver(_client(designer), task, item, _png(1080, 1920))

    item.refresh_from_db()
    task.refresh_from_db()
    assert response.status_code == 200
    assert item.is_done and item.media_asset.width == 1080
    assert task.column.is_review
    assert Notification.objects.filter(user=owner, event_type=EventType.TASK_READY_FOR_REVIEW).exists()
    # The delivered file is protected from the orphaned-media sweep.
    assert item.media_asset_id in _fk_referenced_asset_ids()


def test_wrong_proportions_are_reported_but_accepted(workspace, owner, designer):
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_story"])
    item = task.checklist.get()

    response = _deliver(_client(designer), task, item, _png(1080, 1080))

    toast = json.loads(response["HX-Trigger"])["showToast"]
    assert toast["tone"] == "warn"
    item.refresh_from_db()
    assert item.is_done


def test_a_viewer_cannot_deliver(workspace, owner):
    viewer = _user("viewer@example.com", workspace, Role.VIEWER)
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_story"])

    response = _deliver(_client(viewer), task, task.checklist.get(), _png(1080, 1920))

    assert response.status_code == 403


def test_the_post_publishes_each_art_in_its_own_format(workspace, owner):
    instagram = _connect(workspace, "instagram", "Insta")
    _connect(workspace, "facebook", "Page")
    task = services.request_arts(
        workspace, owner, title="Arts", format_keys=["ig_feed_portrait", "ig_story", "ig_profile"]
    )
    feed_item, story_item, profile_item = task.checklist.all()
    client = _client(owner)
    _deliver(client, task, feed_item, _png(1080, 1350, "feed.png"))
    _deliver(client, task, story_item, _png(1080, 1920, "story.png"))
    _deliver(client, task, profile_item, _png(320, 320, "profile.png"))
    feed_item.refresh_from_db()
    story_item.refresh_from_db()

    response = client.post(reverse("tasks:task_create_post", kwargs={"workspace_id": workspace.id, "task_id": task.id}))

    task.refresh_from_db()
    post = task.post
    assert response["HX-Redirect"] == reverse(
        "composer:compose_edit", kwargs={"workspace_id": workspace.id, "post_id": post.id}
    )
    assert [pm.media_asset_id for pm in post.media_attachments.all()] == [feed_item.media_asset_id]
    rows = {pp.format: pp for pp in PlatformPost.objects.filter(post=post)}
    # Only Instagram: there is no Facebook art, and the profile photo is art only.
    assert set(rows) == {"default", "story"}
    assert all(pp.social_account == instagram for pp in rows.values())
    assert rows["default"].platform_specific_media is None
    assert rows["story"].platform_specific_media == [str(story_item.media_asset_id)]
    story_item.media_asset.refresh_from_db()
    assert story_item.media_asset.derived_from_id == feed_item.media_asset_id


def test_no_post_without_a_publishable_delivered_art(workspace, owner):
    _connect(workspace, "facebook", "Page")
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["fb_cover"])

    response = _client(owner).post(
        reverse("tasks:task_create_post", kwargs={"workspace_id": workspace.id, "task_id": task.id})
    )

    assert "showToast" in response["HX-Trigger"]
    task.refresh_from_db()
    assert task.post is None


def test_the_modal_gets_the_form_and_a_redirect_to_the_new_task(workspace, owner):
    _connect(workspace, "instagram", "Insta")
    client = _client(owner)

    form = client.get(_request_url(workspace), HTTP_HX_REQUEST="true").content.decode()
    invalid = client.post(_request_url(workspace), {"title": "Arts"}, HTTP_HX_REQUEST="true")
    created = client.post(_request_url(workspace), {"title": "Arts", "formats": ["ig_story"]}, HTTP_HX_REQUEST="true")

    assert "<html" not in form and "target-card" in form and "Insta" in form
    assert invalid.status_code == 200 and "Choose at least one format." in invalid.content.decode()
    task = Task.objects.get()
    assert created.status_code == 204 and created["HX-Redirect"].endswith(f"?task={task.pk}")


def test_the_post_uses_the_caption_never_the_briefing(workspace, owner):
    _connect(workspace, "instagram", "Insta")
    task = services.request_arts(
        workspace,
        owner,
        title="Arts",
        format_keys=["ig_feed_square"],
        description="Use the red palette and the new logo",
        post_caption="Our new collection is here!",
    )
    _deliver(_client(owner), task, task.checklist.get(), _png(1080, 1080))

    post = services.create_post_from_task(task, owner)

    assert post.caption == "Our new collection is here!"


def test_an_ideas_text_becomes_the_caption_not_the_briefing(workspace, owner):
    idea = Idea.objects.create(workspace=workspace, title="Launch", description="Launch caption")

    body = _client(owner).get(_request_url(workspace), {"idea": idea.id}, HTTP_HX_REQUEST="true").content.decode()

    briefing = body.split('name="description"')[1].split("</textarea>")[0]
    caption = body.split('name="post_caption"')[1].split("</textarea>")[0]
    assert "Launch caption" in caption
    assert "Launch caption" not in briefing


def test_every_channel_on_offer_is_listed_and_hidden_ones_are_not(workspace, owner):
    from apps.social_accounts.models import PlatformVisibility

    _connect(workspace, "instagram", "Insta")
    PlatformVisibility.objects.update_or_create(platform="pinterest", defaults={"is_visible": False})

    body = _client(owner).get(_request_url(workspace), HTTP_HX_REQUEST="true").content.decode()

    assert "TikTok · not connected" in body and "Bluesky · not connected" in body
    assert "Pinterest" not in body
    # Instagram is connected, so it isn't offered again as not connected.
    assert "Instagram · not connected" not in body


def test_unconnected_choices_treat_both_instagram_routes_as_one_network():
    from apps.social_accounts.models import PlatformVisibility

    offered = dict(PlatformVisibility.unconnected_choices(["instagram_login"]))

    assert "instagram" not in offered and "instagram_login" not in offered
    assert "facebook" in offered


def test_delivered_arts_go_to_the_tasks_folder_of_the_media_library(workspace, owner):
    from apps.media_library.models import MediaFolder

    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_feed_square", "ig_story"])
    square, story = task.checklist.order_by("position")
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    client = _client(owner)

    _deliver(client, task, square, _png(1080, 1080))
    _deliver(client, task, story, _png(1080, 1920))

    folders = MediaFolder.objects.filter(workspace=workspace, parent_folder__isnull=True)
    assert list(folders.values_list("name", flat=True)) == ["Tasks"]
    square.refresh_from_db()
    story.refresh_from_db()
    assert square.media_asset.folder == story.media_asset.folder == folders.get()
    assert not MediaFolder.objects.filter(workspace=other).exists()


def test_the_tasks_folder_is_named_in_the_sites_language(workspace, settings):
    settings.LANGUAGE_CODE = "pt-br"

    assert services.media_folder(workspace).name == "Tarefas"
    assert services.media_folder(workspace) == services.media_folder(workspace)


def _item_url(name, task, item):
    return reverse(f"tasks:{name}", kwargs={"workspace_id": task.workspace_id, "task_id": task.id, "item_id": item.id})


def test_cropping_a_delivered_art_makes_the_crop_the_items_art_in_the_tasks_folder(workspace, owner):
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_feed_portrait"])
    item = task.checklist.get()
    client = _client(owner)
    _deliver(client, task, item, _png(1200, 1200))
    item.refresh_from_db()
    original = item.media_asset

    response = client.post(_item_url("item_art_crop", task, item), {"x": 120, "y": 0, "width": 960, "height": 1200})

    assert response.status_code == 204
    item.refresh_from_db()
    assert item.media_asset != original and (item.media_asset.width, item.media_asset.height) == (960, 1200)
    assert item.media_asset.folder == original.folder == services.media_folder(workspace)
    assert item.is_done
    # A box outside the image is refused in plain text, which the editor shows.
    refused = client.post(_item_url("item_art_crop", task, item), {"x": 0, "y": 0, "width": 5000, "height": 5000})
    assert refused.status_code == 400 and refused["Content-Type"].startswith("text/plain")


def test_removing_a_delivered_art_reopens_the_item_and_keeps_the_file(workspace, owner):
    from apps.media_library.models import MediaAsset

    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_story"])
    item = task.checklist.get()
    client = _client(owner)
    _deliver(client, task, item, _png(1080, 1920))
    item.refresh_from_db()
    asset_id = item.media_asset_id

    client.post(_item_url("item_art_remove", task, item))

    item.refresh_from_db()
    assert item.media_asset is None and not item.is_done
    assert MediaAsset.objects.filter(id=asset_id).exists()


def test_another_workspace_cannot_crop_or_remove_an_art(workspace, owner):
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_story"])
    item = task.checklist.get()
    _deliver(_client(owner), task, item, _png(1080, 1920))
    other = Workspace.objects.create(organization=Organization.objects.create(name="Other org"), name="Other")
    stranger = _user("stranger@example.com", other, Role.OWNER)
    client = _client(stranger)

    for name, data in (("item_art_remove", {}), ("item_art_crop", {"x": 0, "y": 0, "width": 10, "height": 10})):
        url = reverse(f"tasks:{name}", kwargs={"workspace_id": other.id, "task_id": task.id, "item_id": item.id})
        assert client.post(url, data).status_code == 404
    item.refresh_from_db()
    assert item.media_asset is not None


def test_the_request_makes_a_checklist_per_network_and_one_for_the_other_to_dos(workspace, owner):
    _client(owner).post(
        _request_url(workspace),
        {
            "title": "Arts: launch",
            "formats": ["fb_story", "ig_story", "ig_feed_portrait"],
            "extra_items": ["Approve with the client", "  "],
        },
    )

    task = Task.objects.get()
    lists = [(c.title, [(i.text, i.format_key) for i in c.items.order_by("position")]) for c in task.checklists.all()]
    assert lists == [
        ("Instagram", [("Portrait post", "ig_feed_portrait"), ("Story", "ig_story")]),
        ("Facebook", [("Story", "fb_story")]),
        ("Other to-dos", [("Approve with the client", "")]),
    ]
    assert not task.checklist.filter(checklist__isnull=True).exists()


def test_a_request_without_other_to_dos_has_only_the_networks(workspace, owner):
    _client(owner).post(_request_url(workspace), {"title": "Arts", "formats": ["ig_story"]})

    assert list(Task.objects.get().checklists.values_list("title", flat=True)) == ["Instagram"]


def test_a_refused_request_keeps_the_extra_to_dos_typed(workspace, owner):
    body = (
        _client(owner)
        .post(
            _request_url(workspace),
            {"title": "", "formats": ["ig_story"], "extra_items": ["Keep me"]},
            HTTP_HX_REQUEST="true",
        )
        .content.decode()
    )

    assert "Keep me" in body and not Task.objects.exists()


def test_the_form_starts_with_the_default_art_assignee_picked(workspace, owner, designer):
    settings_row = services.art_settings(workspace)
    settings_row.art_assignee = designer
    settings_row.save()

    body = _client(owner).get(_request_url(workspace), HTTP_HX_REQUEST="true").content.decode()

    picked = body.split('data-picked="', 1)[1].split('"', 1)[0]
    assert str(designer.pk) in picked and str(owner.pk) not in picked


def test_a_request_from_the_boards_window_stays_on_the_board(workspace, owner):
    response = _client(owner).post(
        _request_url(workspace), {"title": "Arts", "formats": ["ig_story"], "board": "1"}, HTTP_HX_REQUEST="true"
    )

    assert "HX-Redirect" not in response
    triggers = response["HX-Trigger"]
    assert "taskCreated" in triggers and "tasksChanged" in triggers and "showToast" in triggers
    assert Task.objects.count() == 1


def test_a_request_from_elsewhere_still_opens_the_task_on_the_board(workspace, owner):
    response = _client(owner).post(
        _request_url(workspace), {"title": "Arts", "formats": ["ig_story"]}, HTTP_HX_REQUEST="true"
    )

    assert f"?task={Task.objects.get().pk}" in response["HX-Redirect"]


def test_the_boards_window_form_carries_the_board_mark(workspace, owner):
    body = _client(owner).get(_request_url(workspace) + "?board=1", HTTP_HX_REQUEST="true").content.decode()

    assert 'name="board" value="1"' in body
