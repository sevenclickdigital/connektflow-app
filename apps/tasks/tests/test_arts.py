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
from apps.tasks.models import Task, TaskColumn
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


def test_delivering_every_art_leaves_the_review_to_the_button_which_tells_the_author(workspace, owner, designer):
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_story"], assignees=[designer])
    item = task.checklist.get()

    response = _deliver(_client(designer), task, item, _png(1080, 1920))

    item.refresh_from_db()
    task.refresh_from_db()
    assert response.status_code == 200
    assert item.is_done and item.media_asset.width == 1080
    # Everything delivered, but the task only went from "To do" into production.
    assert not task.column.is_review and task.column.position == 1
    assert not Notification.objects.filter(event_type=EventType.TASK_READY_FOR_REVIEW).exists()

    _client(designer).post(
        reverse("tasks:task_stage", kwargs={"workspace_id": workspace.id, "task_id": task.id}), {"stage": "review"}
    )

    task.refresh_from_db()
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


def _approve(task):
    """Put the task in its board's done column, where its post can be created."""
    done = TaskColumn.objects.get(workspace=task.workspace, is_done=True)
    Task.objects.filter(pk=task.pk).update(column=done)
    task.refresh_from_db()


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
    _approve(task)

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
    _approve(task)

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

    assert "<html" not in form and "art-channel" in form and "Insta" in form
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

    briefing = body.split('name="briefing"')[1].split("</textarea>")[0]
    caption = body.split('name="post_caption"')[1].split("</textarea>")[0]
    assert "Launch caption" in caption
    assert "Launch caption" not in briefing


def test_every_channel_on_offer_is_listed_and_hidden_ones_are_not(workspace, owner):
    from apps.social_accounts.models import PlatformVisibility

    _connect(workspace, "instagram", "Insta")
    PlatformVisibility.objects.update_or_create(platform="pinterest", defaults={"is_visible": False})

    body = _client(owner).get(_request_url(workspace), HTTP_HX_REQUEST="true").content.decode()

    assert "All formats of TikTok" in body and "All formats of Bluesky" in body
    assert "Pinterest" not in body
    # Instagram is listed once, with its connected account.
    assert body.count("All formats of Instagram") == 1
    instagram_row = body.split("All formats of Instagram")[0].rsplit('class="art-channel"', 1)[1]
    assert "Insta" in instagram_row and "Not connected" not in instagram_row


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


def _create_url(workspace):
    return reverse("tasks:task_create", kwargs={"workspace_id": workspace.id})


def test_the_new_task_window_asks_for_arts_with_the_tasks_own_fields(workspace, owner):
    _todo, progress, _review, _changes, _done = services.ensure_columns(workspace)

    response = _client(owner).post(
        _create_url(workspace),
        {
            "title": "Launch",
            "description": "Team notes",
            "request_art": "1",
            "formats": ["ig_story", "ig_feed_portrait", "fb_post_square"],
            "briefing": "Purple, with the product",
            "make_post": "1",
            "post_caption": "It's here!",
            "column": progress.id,
            "priority": "high",
            "tags": ["#launch"],
            "checklists": json.dumps([{"title": "Approval", "items": ["Client OK"]}]),
        },
        HTTP_HX_REQUEST="true",
    )

    assert "HX-Redirect" not in response
    triggers = response["HX-Trigger"]
    assert "taskCreated" in triggers and "tasksChanged" in triggers
    task = Task.objects.get()
    assert (task.description, task.briefing, task.post_caption) == (
        "Team notes",
        "Purple, with the product",
        "It's here!",
    )
    assert (task.column, task.priority, task.tags) == (progress, "high", ["launch"])
    titles = [c.title for c in task.checklists.order_by("position")]
    assert titles[-1] == "Approval" and len(titles) == 3
    assert sorted(task.checklist.exclude(format_key="").values_list("format_key", flat=True)) == [
        "fb_post_square",
        "ig_feed_portrait",
        "ig_story",
    ]


def test_asking_for_arts_needs_a_format(workspace, owner):
    response = _client(owner).post(
        _create_url(workspace), {"title": "Launch", "request_art": "1", "formats": ["nope"]}, HTTP_HX_REQUEST="true"
    )

    assert "showToast" in response["HX-Trigger"] and "taskCreated" not in response["HX-Trigger"]
    assert not Task.objects.exists()


def test_without_the_switch_the_formats_are_ignored(workspace, owner):
    _client(owner).post(_create_url(workspace), {"title": "Plain", "formats": ["ig_story"]}, HTTP_HX_REQUEST="true")

    task = Task.objects.get()
    assert not task.checklist.exists() and task.briefing == ""


def test_a_column_of_another_workspace_is_refused(workspace, owner):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    foreign = services.ensure_columns(other)[1]

    response = _client(owner).post(
        _create_url(workspace),
        {"title": "Arts", "request_art": "1", "formats": ["ig_story"], "column": foreign.id},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 404
    assert not Task.objects.filter(workspace=workspace).exists()


def test_the_board_offers_the_networks_in_the_new_task_window(workspace, owner):
    _connect(workspace, "instagram", "Insta")

    body = _client(owner).get(reverse("tasks:board", kwargs={"workspace_id": workspace.id})).content.decode()

    assert 'name="request_art"' in body and 'name="formats" value="ig_story"' in body


def test_an_art_task_shows_its_briefing_in_its_window(workspace, owner):
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_story"], briefing="Bold type")

    body = (
        _client(owner)
        .get(
            reverse("tasks:task_detail", kwargs={"workspace_id": workspace.id, "task_id": task.id}),
            HTTP_HX_REQUEST="true",
        )
        .content.decode()
    )

    assert 'name="briefing"' in body and "Bold type</textarea>" in body


def test_a_request_from_elsewhere_still_opens_the_task_on_the_board(workspace, owner):
    response = _client(owner).post(
        _request_url(workspace), {"title": "Arts", "formats": ["ig_story"]}, HTTP_HX_REQUEST="true"
    )

    assert f"?task={Task.objects.get().pk}" in response["HX-Redirect"]


def test_the_new_task_window_makes_a_post_task_with_its_caption_and_channels(workspace, owner):
    instagram = _connect(workspace, "instagram", "Insta")
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    foreign = _connect(other, "facebook", "Not ours")

    _client(owner).post(
        _create_url(workspace),
        {
            "title": "Launch post",
            "make_post": "1",
            "post_caption": "It's here!",
            "channel_formats": json.dumps({str(instagram.id): ["story", "nope"], str(foreign.id): ["default"]}),
        },
        HTTP_HX_REQUEST="true",
    )

    task = Task.objects.get(workspace=workspace)
    assert task.post_caption == "It's here!"
    # Only the brand's own account, on the formats it has.
    assert task.channel_formats == {str(instagram.id): ["story"]}


def test_without_the_post_switch_no_caption_nor_channels_are_kept(workspace, owner):
    instagram = _connect(workspace, "instagram", "Insta")

    _client(owner).post(
        _create_url(workspace),
        {"title": "Plain", "post_caption": "Ignored", "channel_formats": json.dumps({str(instagram.id): ["default"]})},
        HTTP_HX_REQUEST="true",
    )

    task = Task.objects.get()
    assert (task.post_caption, task.channel_formats) == ("", {})


def test_the_post_goes_out_only_on_the_tasks_channels(workspace, owner):
    instagram = _connect(workspace, "instagram", "Insta")
    task = services.request_arts(
        workspace,
        owner,
        title="Arts",
        format_keys=["ig_feed_portrait", "ig_story"],
        channel_formats={str(instagram.id): ["story"]},
    )
    feed_item, story_item = task.checklist.all()
    client = _client(owner)
    _deliver(client, task, feed_item, _png(1080, 1350, "feed.png"))
    _deliver(client, task, story_item, _png(1080, 1920, "story.png"))

    post = services.create_post_from_task(Task.objects.get(pk=task.pk), owner)

    assert [pp.format for pp in PlatformPost.objects.filter(post=post)] == ["story"]


def test_the_task_window_saves_the_posts_channels(workspace, owner):
    instagram = _connect(workspace, "instagram", "Insta")
    task = services.create_task(workspace, owner, title="Post")

    _client(owner).post(
        reverse("tasks:task_update", kwargs={"workspace_id": workspace.id, "task_id": task.id}),
        {"save": "1", "channel_formats": json.dumps({str(instagram.id): ["default"]})},
    )

    task.refresh_from_db()
    assert task.channel_formats == {str(instagram.id): ["default"]}


def test_ticking_an_item_moves_the_task_from_to_do_to_in_progress(workspace, owner):
    todo, progress, _review, _changes, _done = services.ensure_columns(workspace)
    task = services.create_task(
        workspace, owner, title="Work", column=todo, checklists=[{"title": "A", "items": ["x", "y"]}]
    )

    services.set_item_done(task.checklist.first(), True, owner)

    task.refresh_from_db()
    assert task.column == progress


def test_ticking_an_item_elsewhere_leaves_the_task_where_it_is(workspace, owner):
    _todo, _progress, review, _changes, _done = services.ensure_columns(workspace)
    task = services.create_task(
        workspace, owner, title="Work", column=review, checklists=[{"title": "A", "items": ["x"]}]
    )

    services.set_item_done(task.checklist.first(), True, owner)

    task.refresh_from_db()
    assert task.column == review


def test_the_task_window_asks_for_more_arts_without_repeating_the_ones_it_has(workspace, owner):
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_story"])

    _client(owner).post(
        reverse("tasks:task_update", kwargs={"workspace_id": workspace.id, "task_id": task.id}),
        {"save": "1", "request_art": "1", "briefing": "Bold", "formats": ["ig_story", "ig_feed_portrait", "fb_story"]},
    )

    task.refresh_from_db()
    assert task.briefing == "Bold"
    keys = sorted(task.checklist.exclude(format_key="").values_list("format_key", flat=True))
    assert keys == ["fb_story", "ig_feed_portrait", "ig_story"]
    # The new Instagram art joins the Instagram checklist; Facebook gets its own.
    assert sorted(task.checklists.values_list("title", flat=True)) == ["Facebook", "Instagram"]


def test_switching_the_post_off_in_the_task_window_clears_caption_and_channels(workspace, owner):
    instagram = _connect(workspace, "instagram", "Insta")
    task = services.create_task(
        workspace, owner, title="Post", post_caption="Hi", channel_formats={str(instagram.id): ["default"]}
    )

    _client(owner).post(
        reverse("tasks:task_update", kwargs={"workspace_id": workspace.id, "task_id": task.id}),
        {"save": "1", "post_sent": "1"},
    )

    task.refresh_from_db()
    assert (task.post_caption, task.channel_formats) == ("", {})


def test_send_for_review_moves_the_card_and_closes_the_window(workspace, owner):
    _todo, progress, review, _changes, _done = services.ensure_columns(workspace)
    task = services.create_task(workspace, owner, title="Work", column=progress)

    response = _client(owner).post(
        reverse("tasks:task_stage", kwargs={"workspace_id": workspace.id, "task_id": task.id}), {"stage": "review"}
    )

    task.refresh_from_db()
    assert task.column == review
    assert "closeTask" in response["HX-Trigger"]


def test_a_task_in_review_is_not_sent_again(workspace, owner):
    _todo, _progress, review, _changes, _done = services.ensure_columns(workspace)
    task = services.create_task(workspace, owner, title="Work", column=review)

    response = _client(owner).post(
        reverse("tasks:task_stage", kwargs={"workspace_id": workspace.id, "task_id": task.id}), {"stage": "review"}
    )
    body = (
        _client(owner)
        .get(
            reverse("tasks:task_detail", kwargs={"workspace_id": workspace.id, "task_id": task.id}),
            HTTP_HX_REQUEST="true",
        )
        .content.decode()
    )

    assert "closeTask" not in response["HX-Trigger"]
    assert '"stage": "review"' not in body
    # In review, the window offers to send it back for changes or approve it.
    assert '"stage": "changes"' in body and '"stage": "done"' in body


def test_another_workspaces_task_cannot_be_sent_for_review(workspace, owner):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    foreign = services.create_task(other, owner, title="Theirs")

    response = _client(owner).post(
        reverse("tasks:task_stage", kwargs={"workspace_id": workspace.id, "task_id": foreign.id}), {"stage": "review"}
    )

    assert response.status_code == 404


def test_a_task_in_review_goes_back_for_changes_or_on_to_done(workspace, owner):
    _todo, _progress, review, changes, done = services.ensure_columns(workspace)
    back = services.create_task(workspace, owner, title="Back", column=review)
    ahead = services.create_task(workspace, owner, title="Ahead", column=review)
    url = lambda task: reverse("tasks:task_stage", kwargs={"workspace_id": workspace.id, "task_id": task.id})  # noqa: E731

    _client(owner).post(url(back), {"stage": "changes"})
    _client(owner).post(url(ahead), {"stage": "done"})

    back.refresh_from_db()
    ahead.refresh_from_db()
    assert back.column == changes and ahead.column == done and ahead.completed_at is not None


def test_a_task_not_in_review_cannot_be_approved(workspace, owner):
    todo, *_rest = services.ensure_columns(workspace)
    task = services.create_task(workspace, owner, title="Early", column=todo)

    response = _client(owner).post(
        reverse("tasks:task_stage", kwargs={"workspace_id": workspace.id, "task_id": task.id}), {"stage": "done"}
    )

    task.refresh_from_db()
    assert task.column == todo and "closeTask" not in response["HX-Trigger"]


def test_a_task_to_do_starts_production_instead_of_going_to_review(workspace, owner):
    todo, progress, *_rest = services.ensure_columns(workspace)
    task = services.create_task(workspace, owner, title="Fresh", column=todo)
    url = reverse("tasks:task_stage", kwargs={"workspace_id": workspace.id, "task_id": task.id})

    body = (
        _client(owner)
        .get(
            reverse("tasks:task_detail", kwargs={"workspace_id": workspace.id, "task_id": task.id}),
            HTTP_HX_REQUEST="true",
        )
        .content.decode()
    )
    refused = _client(owner).post(url, {"stage": "review"})
    _client(owner).post(url, {"stage": "start"})

    assert '"stage": "start"' in body and '"stage": "review"' not in body
    assert "closeTask" not in refused["HX-Trigger"]
    task.refresh_from_db()
    assert task.column == progress


def _comment_url(task, checklist):
    return reverse(
        "tasks:checklist_comment",
        kwargs={"workspace_id": task.workspace_id, "task_id": task.id, "checklist_id": checklist.id},
    )


def test_a_task_in_review_takes_comments_on_its_checklists_and_shows_them(workspace, owner):
    _todo, _progress, review, *_rest = services.ensure_columns(workspace)
    task = services.create_task(
        workspace, owner, title="Arts", column=review, checklists=[{"title": "Instagram", "items": ["Feed"]}]
    )
    checklist = task.checklists.get()

    body = _client(owner).post(_comment_url(task, checklist), {"body": "  Make the logo bigger  "}).content.decode()
    _client(owner).post(_comment_url(task, checklist), {"body": "   "})

    comment = checklist.comments.get()
    assert (comment.body, comment.author) == ("Make the logo bigger", owner)
    assert "Make the logo bigger" in body


def test_comments_are_written_only_in_review(workspace, owner):
    todo, *_rest = services.ensure_columns(workspace)
    task = services.create_task(
        workspace, owner, title="Early", column=todo, checklists=[{"title": "A", "items": ["x"]}]
    )

    response = _client(owner).post(_comment_url(task, task.checklists.get()), {"body": "Too soon"})

    assert "showToast" in response["HX-Trigger"]
    assert not task.checklists.get().comments.exists()


def test_another_workspaces_checklist_cannot_be_commented(workspace, owner):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    review = services.ensure_columns(other)[2]
    foreign = services.create_task(
        other, owner, title="Theirs", column=review, checklists=[{"title": "A", "items": ["x"]}]
    )
    checklist = foreign.checklists.get()

    response = _client(owner).post(
        reverse(
            "tasks:checklist_comment",
            kwargs={"workspace_id": workspace.id, "task_id": foreign.id, "checklist_id": checklist.id},
        ),
        {"body": "Sneaky"},
    )

    assert response.status_code == 404
    assert not checklist.comments.exists()


def test_the_task_window_sums_up_the_arts_and_the_posts_channels(workspace, owner):
    instagram = _connect(workspace, "instagram", "Insta")
    task = services.request_arts(
        workspace,
        owner,
        title="Launch",
        format_keys=["ig_story"],
        briefing="Bold type",
        post_caption="It's here",
        channel_formats={str(instagram.id): ["story"]},
    )

    body = (
        _client(owner)
        .get(
            reverse("tasks:task_detail", kwargs={"workspace_id": workspace.id, "task_id": task.id}),
            HTTP_HX_REQUEST="true",
        )
        .content.decode()
    )

    summary = body.split("Arts requested")[1]
    assert "Bold type" in summary and "Instagram:" in summary
    assert "Insta</span>" in summary and "It&#x27;s here" in summary


def test_the_post_waits_for_the_task_to_be_approved(workspace, owner):
    _connect(workspace, "instagram", "Insta")
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_feed_portrait"])
    _deliver(_client(owner), task, task.checklist.get(), _png(1080, 1350))

    response = _client(owner).post(
        reverse("tasks:task_create_post", kwargs={"workspace_id": workspace.id, "task_id": task.id})
    )
    body = (
        _client(owner)
        .get(
            reverse("tasks:task_detail", kwargs={"workspace_id": workspace.id, "task_id": task.id}),
            HTTP_HX_REQUEST="true",
        )
        .content.decode()
    )

    task.refresh_from_db()
    assert task.post is None and "showToast" in response["HX-Trigger"]
    assert "create-post" not in body
    _approve(task)
    body = (
        _client(owner)
        .get(
            reverse("tasks:task_detail", kwargs={"workspace_id": workspace.id, "task_id": task.id}),
            HTTP_HX_REQUEST="true",
        )
        .content.decode()
    )
    assert "create-post" in body


def _image_asset(workspace, name="ref.png"):
    from apps.media_library.models import MediaAsset

    return MediaAsset.objects.create(
        workspace=workspace, file=SimpleUploadedFile(name, b"x"), filename=name, media_type=MediaAsset.MediaType.IMAGE
    )


def test_an_art_request_keeps_its_reference_photos_only_the_workspaces_images(workspace, owner):
    mine = _image_asset(workspace)
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    theirs = _image_asset(other, "theirs.png")

    _client(owner).post(
        _create_url(workspace),
        {
            "title": "Arts",
            "request_art": "1",
            "formats": ["ig_story"],
            "references": [str(mine.id), str(theirs.id), "nope"],
        },
        HTTP_HX_REQUEST="true",
    )

    task = Task.objects.get(workspace=workspace)
    assert list(task.references.all()) == [mine]
    # Kept out of the orphaned-media sweep.
    assert mine.id in _fk_referenced_asset_ids()


def test_the_task_window_replaces_the_reference_photos(workspace, owner):
    first, second = _image_asset(workspace, "a.png"), _image_asset(workspace, "b.png")
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_story"], references=[first.id])

    _client(owner).post(
        reverse("tasks:task_update", kwargs={"workspace_id": workspace.id, "task_id": task.id}),
        {"save": "1", "request_art": "1", "references_sent": "1", "references": [str(second.id)]},
    )

    assert list(task.references.all()) == [second]


def test_an_art_is_delivered_from_the_media_library_but_never_another_workspaces(workspace, owner):
    task = services.request_arts(workspace, owner, title="Arts", format_keys=["ig_story"])
    item = task.checklist.get()
    url = reverse("tasks:item_deliver", kwargs={"workspace_id": workspace.id, "task_id": task.id, "item_id": item.id})
    other = Workspace.objects.create(organization=workspace.organization, name="Other")

    refused = _client(owner).post(url, {"asset_id": str(_image_asset(other, "theirs.png").id)})
    item.refresh_from_db()
    assert "showToast" in refused["HX-Trigger"] and item.media_asset is None

    mine = _image_asset(workspace)
    _client(owner).post(url, {"asset_id": str(mine.id)})
    item.refresh_from_db()
    assert item.media_asset == mine and item.is_done


def test_an_item_set_to_ask_for_a_file_takes_a_delivery_of_any_size(workspace, owner):
    task = services.create_task(
        workspace, owner, title="Plain", checklists=[{"title": "A", "items": ["Product photo"]}]
    )
    item = task.checklist.get()
    settings_url = reverse(
        "tasks:item_settings", kwargs={"workspace_id": workspace.id, "task_id": task.id, "item_id": item.id}
    )
    deliver_url = reverse(
        "tasks:item_deliver", kwargs={"workspace_id": workspace.id, "task_id": task.id, "item_id": item.id}
    )

    refused = _client(owner).post(deliver_url, {"asset_id": str(_image_asset(workspace).id)})
    _client(owner).post(settings_url, {"accepts_file": "1", "width": "abc", "height": "900"})
    item.refresh_from_db()
    assert "showToast" in refused["HX-Trigger"]
    # A size needs both sides: one invalid side clears it.
    assert item.accepts_file and (item.width, item.height) == (None, None)

    _client(owner).post(deliver_url, {"asset_id": str(_image_asset(workspace, "photo.png").id)})
    item.refresh_from_db()
    assert item.media_asset is not None and item.is_done


def test_an_items_size_is_set_in_its_settings_and_checked_on_delivery(workspace, owner):
    task = services.create_task(workspace, owner, title="Plain", checklists=[{"title": "A", "items": ["Banner"]}])
    item = task.checklist.get()

    _client(owner).post(
        reverse("tasks:item_settings", kwargs={"workspace_id": workspace.id, "task_id": task.id, "item_id": item.id}),
        {"accepts_file": "1", "width": "1200", "height": "600"},
    )
    response = _deliver(_client(owner), task, item, _png(1080, 1080))

    item.refresh_from_db()
    assert (item.width, item.height) == (1200, 600)
    assert json.loads(response["HX-Trigger"])["showToast"]["tone"] == "warn"


def test_the_new_task_windows_items_carry_their_settings(workspace, owner):
    _client(owner).post(
        _create_url(workspace),
        {
            "title": "Shoot",
            "checklists": json.dumps(
                [
                    {
                        "title": "Photos",
                        "items": [
                            "Plain",
                            {"text": "Product photo", "accepts_file": True, "width": "1080", "height": "1350"},
                            {"text": "Half size", "accepts_file": False, "width": "900", "height": ""},
                        ],
                    }
                ]
            ),
        },
        HTTP_HX_REQUEST="true",
    )

    items = {i.text: i for i in Task.objects.get().checklist.all()}
    assert (items["Plain"].accepts_file, items["Plain"].width) == (False, None)
    assert (items["Product photo"].accepts_file, items["Product photo"].width, items["Product photo"].height) == (
        True,
        1080,
        1350,
    )
    assert (items["Half size"].accepts_file, items["Half size"].width) == (False, None)
