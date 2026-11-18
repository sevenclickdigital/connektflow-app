"""Who may change what through the composer's endpoints.

Membership of the workspace opens the read-only views; every mutation needs the
permission its role grants, a post is changed only by its author or a member
with ``edit_others_posts``, and — in a workspace whose approval is required —
a member without ``publish_directly`` schedules only content a reviewer cleared.
"""

import uuid
from datetime import time, timedelta
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.calendar.models import PostingSlot, Queue
from apps.composer import urls as composer_urls
from apps.composer.models import ContentCategory, PlatformPost, Post, PostMedia, PostTemplate
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

# Routes that answer a POST from any member: they render and write nothing.
READ_ONLY_POST_ROUTES = {"preview", "category_list", "template_list"}


class ComposerAuthorizationBase(TestCase):
    approval_mode = "none"

    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.ws = Workspace.objects.create(
            organization=self.org, name="WS", timezone="UTC", approval_workflow_mode=self.approval_mode
        )
        self.account = SocialAccount.objects.create(
            workspace=self.ws,
            platform="linkedin_personal",
            account_platform_id="li-1",
            account_name="LinkedIn",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.manager = self._member("manager")
        self.contributor = self._member("contributor")
        self.when = timezone.now() + timedelta(days=2)

    def _member(self, role, email=None):
        user = User.objects.create_user(
            email=email or f"{role}@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.create(user=user, organization=self.org, org_role=OrgMembership.OrgRole.MEMBER)
        WorkspaceMembership.objects.create(user=user, workspace=self.ws, workspace_role=role)
        return user

    def _url(self, name, **kwargs):
        return reverse(f"composer:{name}", kwargs={"workspace_id": self.ws.id, **kwargs})

    def _post(self, author, status="draft", caption="original caption"):
        post = Post.objects.create(workspace=self.ws, author=author, caption=caption)
        row = PlatformPost.objects.create(post=post, social_account=self.account, status=status, scheduled_at=self.when)
        return post, row

    def _asset(self, name="a.jpg", workspace=None):
        workspace = workspace or self.ws
        asset = MediaAsset(
            organization=workspace.organization,
            workspace=workspace,
            filename=name,
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/jpeg",
        )
        asset.file.save(name, ContentFile(b"\xff\xd8\xff\xe0" + name.encode()), save=False)
        asset.save()
        return asset

    def _form(self, action="save_draft", caption="original caption", **extra):
        data = {
            "action": action,
            "title": "",
            "caption": caption,
            "tags": "",
            "selected_accounts": str(self.account.id),
            "scheduled_date": self.when.strftime("%Y-%m-%d"),
            "scheduled_time": "09:00",
        }
        data.update(extra)
        return data


class RoleGateTests(ComposerAuthorizationBase):
    def setUp(self):
        super().setUp()
        self.viewer = self._member("viewer")

    def test_viewer_is_refused_on_every_mutating_route(self):
        post, row = self._post(self.manager, "scheduled")
        self.client.force_login(self.viewer)
        known = {"post_id": post.id, "platform_post_id": row.id}
        checked = 0
        for pattern in composer_urls.urlpatterns:
            if pattern.name in READ_ONLY_POST_ROUTES:
                continue
            kwargs = {
                name: known.get(name, 1 if type(converter).__name__ == "IntConverter" else uuid.uuid4())
                for name, converter in pattern.pattern.converters.items()
            }
            response = self.client.post(self._url(pattern.name, **kwargs))
            # 405 is a GET-only view, which a POST cannot make write anything.
            self.assertIn(response.status_code, (403, 405), pattern.name)
            checked += 1
        self.assertGreater(checked, 60)
        self.assertTrue(Post.objects.filter(id=post.id).exists())
        row.refresh_from_db()
        self.assertEqual(row.status, "scheduled")

    def test_client_role_cannot_delete_a_post(self):
        post, _row = self._post(self.manager, "published")
        self.client.force_login(self._member("client"))
        response = self.client.post(self._url("post_delete", post_id=post.id))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Post.objects.filter(id=post.id).exists())

    def test_contributor_cannot_delete_or_unschedule_another_authors_post(self):
        post, row = self._post(self.manager, "scheduled")
        attachment = PostMedia.objects.create(post=post, media_asset=self._asset(), position=0)
        self.client.force_login(self.contributor)

        deleted = self.client.post(self._url("post_delete", post_id=post.id))
        unscheduled = self.client.post(
            self._url("transition_platform_post", post_id=post.id, platform_post_id=row.id),
            {"target_status": "draft"},
        )
        removed = self.client.post(self._url("remove_media", post_id=post.id, media_id=attachment.id))
        attached = self.client.post(
            self._url("attach_media", post_id=post.id), {"media_asset_id": str(self._asset("b.jpg").id)}
        )

        self.assertEqual(
            (deleted.status_code, unscheduled.status_code, removed.status_code, attached.status_code),
            (403, 403, 403, 403),
        )
        row.refresh_from_db()
        self.assertEqual(row.status, "scheduled")
        self.assertEqual(list(post.media_attachments.values_list("id", flat=True)), [attachment.id])

    def test_author_deletes_and_unschedules_their_own_post(self):
        post, row = self._post(self.contributor, "scheduled")
        self.client.force_login(self.contributor)

        unscheduled = self.client.post(
            self._url("transition_platform_post", post_id=post.id, platform_post_id=row.id),
            {"target_status": "draft"},
        )
        self.assertEqual(unscheduled.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.status, "draft")

        deleted = self.client.post(self._url("post_delete", post_id=post.id))
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(Post.objects.filter(id=post.id).exists())

    def test_reviewer_decides_on_another_authors_post_through_the_chip(self):
        post, row = self._post(self.contributor, "pending_review")
        self.client.force_login(self.manager)
        response = self.client.post(
            self._url("transition_platform_post", post_id=post.id, platform_post_id=row.id),
            {"target_status": "approved"},
        )
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.status, "approved")

    def test_remove_pending_media_never_deletes_a_library_asset_named_by_id(self):
        asset = self._asset()
        self.client.force_login(self.manager)
        response = self.client.post(self._url("remove_pending_media", asset_id=asset.id))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(MediaAsset.objects.filter(id=asset.id).exists())

    def test_remove_pending_media_deletes_what_the_session_was_holding(self):
        asset = self._asset()
        self.client.force_login(self.contributor)
        session = self.client.session
        session[f"pending_media_{self.ws.id}"] = [str(asset.id)]
        session.save()
        response = self.client.post(self._url("remove_pending_media", asset_id=asset.id))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MediaAsset.objects.filter(id=asset.id).exists())

    def test_contributor_keeps_categories_templates_and_tags(self):
        self.client.force_login(self.contributor)
        template = PostTemplate.objects.create(workspace=self.ws, name="T", template_data={})
        created = self.client.post(self._url("category_create"), {"name": "Educational", "color": "#3B82F6"})
        tagged = self.client.post(self._url("tag_create"), {"name": "launch"})
        removed = self.client.post(self._url("template_delete", template_id=template.id))
        self.assertEqual((created.status_code, tagged.status_code, removed.status_code), (204, 200, 204))
        self.assertTrue(ContentCategory.objects.filter(workspace=self.ws, name="Educational").exists())
        self.assertFalse(PostTemplate.objects.filter(id=template.id).exists())

    def test_upload_needs_upload_media(self):
        membership = WorkspaceMembership.objects.get(user=self.contributor, workspace=self.ws)
        self.assertTrue(membership.effective_permissions["upload_media"])
        self.client.force_login(self.viewer)
        response = self.client.post(
            self._url("upload_media"), {"file": SimpleUploadedFile("a.txt", b"x", content_type="text/plain")}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MediaAsset.objects.filter(workspace=self.ws).exists())


class ScheduleNeedsApprovalTests(ComposerAuthorizationBase):
    approval_mode = "required_internal"

    def setUp(self):
        super().setUp()
        self.client.force_login(self.contributor)

    def _statuses(self):
        return list(PlatformPost.objects.filter(post__workspace=self.ws).values_list("status", flat=True))

    def _queue(self):
        for day in range(7):
            PostingSlot.objects.create(social_account=self.account, day_of_week=day, time=time(9, 0))
        return Queue.objects.create(workspace=self.ws, name="Q", social_account=self.account)

    def test_contributor_cannot_schedule_a_new_post(self):
        response = self.client.post(self._url("save_post"), self._form("schedule"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("schedule", response.json()["errors"])
        self.assertFalse(Post.objects.filter(workspace=self.ws).exists())

    def test_contributor_cannot_queue_a_new_post(self):
        queue = self._queue()
        for action in ("add_to_queue", "add_to_queue_priority"):
            response = self.client.post(self._url("save_post"), self._form(action, queue_id=str(queue.id)))
            self.assertEqual(response.status_code, 403, action)
        self.assertFalse(Post.objects.filter(workspace=self.ws).exists())

    def test_contributor_cannot_schedule_a_draft_or_rejected_post(self):
        queue = self._queue()
        for status in ("draft", "rejected", "changes_requested", "pending_review"):
            post, row = self._post(self.contributor, status)
            for action in ("schedule", "add_to_queue", "add_to_queue_priority"):
                response = self.client.post(
                    self._url("save_post_edit", post_id=post.id), self._form(action, queue_id=str(queue.id))
                )
                self.assertEqual(response.status_code, 403, (status, action))
                row.refresh_from_db()
                self.assertEqual(row.status, status, action)

    def test_author_schedules_an_approved_post(self):
        post, row = self._post(self.contributor, "approved")
        response = self.client.post(self._url("save_post_edit", post_id=post.id), self._form("schedule"))
        self.assertIn(response.status_code, (204, 302))
        row.refresh_from_db()
        self.assertEqual(row.status, "scheduled")

    def test_author_queues_an_approved_post(self):
        queue = self._queue()
        post, row = self._post(self.contributor, "approved")
        response = self.client.post(
            self._url("save_post_edit", post_id=post.id), self._form("add_to_queue", queue_id=str(queue.id))
        )
        self.assertIn(response.status_code, (204, 302))
        row.refresh_from_db()
        self.assertEqual(row.status, "scheduled")

    def test_account_added_while_scheduling_stays_a_draft(self):
        other = SocialAccount.objects.create(
            workspace=self.ws,
            platform="bluesky",
            account_platform_id="bs-1",
            account_name="Bluesky",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        post, row = self._post(self.contributor, "approved")
        self.client.post(
            self._url("save_post_edit", post_id=post.id),
            self._form("schedule", selected_accounts=f"{self.account.id},{other.id}"),
        )
        row.refresh_from_db()
        self.assertEqual(row.status, "scheduled")
        self.assertEqual(post.platform_posts.get(social_account=other).status, "draft")

    def test_manager_schedules_a_new_post(self):
        self.client.force_login(self.manager)
        response = self.client.post(self._url("save_post"), self._form("schedule"))
        self.assertIn(response.status_code, (204, 302))
        self.assertEqual(self._statuses(), ["scheduled"])

    def _import_csv(self):
        body = f"caption,date,time,platforms\nfrom csv,{self.when:%Y-%m-%d},09:00,linkedin_personal\n".encode()
        self.client.post(
            self._url("csv_upload"), {"csv_file": SimpleUploadedFile("p.csv", body, content_type="text/csv")}
        )
        preview = self.client.post(
            self._url("csv_preview"), {"map_caption": "0", "map_date": "1", "map_time": "2", "map_platforms": "3"}
        )
        self.assertEqual(preview.status_code, 200)
        confirm = self.client.post(self._url("csv_confirm_import"))
        self.assertEqual(confirm.status_code, 200)
        return Post.objects.get(workspace=self.ws, caption="from csv")

    def test_csv_import_by_a_contributor_creates_drafts(self):
        post = self._import_csv()
        self.assertEqual(self._statuses(), ["draft"])
        self.assertIsNone(post.scheduled_at)
        self.assertIsNotNone(post.proposed_publish_at)

    def test_csv_import_by_a_manager_schedules(self):
        self.client.force_login(self.manager)
        post = self._import_csv()
        self.assertEqual(self._statuses(), ["scheduled"])
        self.assertIsNotNone(post.scheduled_at)


class ScheduleWithoutRequiredApprovalTests(ComposerAuthorizationBase):
    def test_contributor_schedules_where_approval_is_not_required(self):
        self.client.force_login(self.contributor)
        response = self.client.post(self._url("save_post"), self._form("schedule"))
        self.assertIn(response.status_code, (204, 302))
        self.assertEqual(PlatformPost.objects.get(post__workspace=self.ws).status, "scheduled")


class EditedContentGoesBackToReviewTests(ComposerAuthorizationBase):
    approval_mode = "required_internal"

    def setUp(self):
        super().setUp()
        self.client.force_login(self.contributor)

    def _save(self, post, **form):
        return self.client.post(self._url("save_post_edit", post_id=post.id), self._form(**form))

    def test_platform_caption_on_an_approved_row_needs_a_new_review(self):
        post, row = self._post(self.contributor, "approved")
        self._save(post, **{f"override_caption_{self.account.id}": "text no reviewer saw"})
        row.refresh_from_db()
        self.assertEqual(row.status, "pending_review")

    def test_platform_caption_cannot_be_scheduled_in_the_same_save(self):
        post, row = self._post(self.contributor, "approved")
        for action in ("schedule", "add_to_queue"):
            self._save(post, action=action, **{f"override_caption_{self.account.id}": "text no reviewer saw"})
            row.refresh_from_db()
            self.assertEqual(row.status, "pending_review", action)

    def test_editing_a_scheduled_post_takes_it_out_of_the_publish_path(self):
        post, row = self._post(self.contributor, "scheduled")
        self._save(post, caption="rewritten after approval")
        row.refresh_from_db()
        self.assertEqual(row.status, "pending_review")

    def test_autosave_of_a_scheduled_post_takes_it_out_of_the_publish_path(self):
        post, row = self._post(self.contributor, "scheduled")
        self.client.post(
            self._url("autosave_edit", post_id=post.id),
            {"title": "", "caption": "rewritten after approval", "selected_accounts": str(self.account.id)},
        )
        row.refresh_from_db()
        self.assertEqual(row.status, "pending_review")

    def test_new_media_on_a_scheduled_post_takes_it_out_of_the_publish_path(self):
        post, row = self._post(self.contributor, "scheduled")
        response = self.client.post(
            self._url("attach_media", post_id=post.id), {"media_asset_id": str(self._asset().id)}
        )
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.status, "pending_review")

    def test_unchanged_save_keeps_a_scheduled_post_scheduled(self):
        post, row = self._post(self.contributor, "scheduled")
        self._save(post)
        self._save(post, action="schedule", scheduled_time="10:30")
        row.refresh_from_db()
        self.assertEqual(row.status, "scheduled")
        self.assertEqual(row.scheduled_at.strftime("%H:%M"), "10:30")

    def test_manager_edits_a_scheduled_post_in_place(self):
        post, row = self._post(self.manager, "scheduled")
        self.client.force_login(self.manager)
        self._save(post, caption="fixed a typo")
        row.refresh_from_db()
        self.assertEqual(row.status, "scheduled")

    def test_adding_an_account_does_not_send_the_approved_one_back(self):
        other = SocialAccount.objects.create(
            workspace=self.ws,
            platform="bluesky",
            account_platform_id="bs-1",
            account_name="Bluesky",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        post, row = self._post(self.contributor, "approved")
        self._save(post, selected_accounts=f"{self.account.id},{other.id}")
        row.refresh_from_db()
        self.assertEqual(row.status, "approved")


class AutosaveBodyPostIdTests(ComposerAuthorizationBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.contributor)

    def _autosave(self, post, caption="rewritten"):
        return self.client.post(
            self._url("autosave"),
            {
                "_autosave_post_id": str(post.id),
                "title": "",
                "caption": caption,
                "selected_accounts": str(self.account.id),
            },
        )

    def test_another_authors_post_is_refused(self):
        post, _row = self._post(self.manager)
        response = self._autosave(post)
        self.assertEqual(response.status_code, 403)
        post.refresh_from_db()
        self.assertEqual(post.caption, "original caption")

    def test_published_post_is_not_rewritten(self):
        post, _row = self._post(self.contributor, "published")
        response = self._autosave(post)
        self.assertEqual(response.status_code, 200)
        post.refresh_from_db()
        self.assertEqual(post.caption, "original caption")

    def test_edit_of_an_approved_post_goes_back_to_review(self):
        post, row = self._post(self.contributor, "approved")
        self._autosave(post)
        row.refresh_from_db()
        self.assertEqual(row.status, "pending_review")

    def test_own_draft_is_saved(self):
        post, _row = self._post(self.contributor)
        response = self._autosave(post)
        self.assertEqual(response.status_code, 200)
        post.refresh_from_db()
        self.assertEqual(post.caption, "rewritten")
        self.assertEqual(Post.objects.filter(workspace=self.ws).count(), 1)

    def test_malformed_id_starts_a_new_draft(self):
        response = self.client.post(
            self._url("autosave"), {"_autosave_post_id": "not-a-uuid", "title": "", "caption": "fresh"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Post.objects.get(workspace=self.ws).author, self.contributor)


class _FakeResponse:
    status_code = 200

    def __init__(self, headers=None, body=None):
        self.headers = headers or {}
        self._body = body or {}

    def json(self):
        return self._body


class ThumbnailAssetScopeTests(ComposerAuthorizationBase):
    def setUp(self):
        super().setUp()
        self.youtube = SocialAccount.objects.create(
            workspace=self.ws,
            platform="youtube",
            account_platform_id="UC-1",
            account_name="Channel",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
            oauth_access_token="token",
        )
        other_org = Organization.objects.create(name="Other org")
        other_ws = Workspace.objects.create(organization=other_org, name="Other WS")
        self.foreign = self._asset("foreign.jpg", workspace=other_ws)
        self.own = self._asset("own.jpg")
        self.client.force_login(self.manager)

    def _save_with_thumbnail(self, asset_id):
        self.client.post(
            self._url("save_post"),
            {
                "action": "save_draft",
                "caption": "video",
                "selected_accounts": str(self.youtube.id),
                f"yt_thumbnail_asset_id_{self.youtube.id}": str(asset_id),
                f"yt_privacy_status_{self.youtube.id}": "private",
            },
        )
        return PlatformPost.objects.get(post__workspace=self.ws, social_account=self.youtube)

    def test_composer_drops_another_tenants_thumbnail(self):
        row = self._save_with_thumbnail(self.foreign.id)
        self.assertIsNone(row.platform_extra["thumbnail_asset_id"])

    def test_composer_keeps_the_workspaces_own_thumbnail(self):
        row = self._save_with_thumbnail(self.own.id)
        self.assertEqual(row.platform_extra["thumbnail_asset_id"], str(self.own.id))

    def test_composer_drops_another_tenants_pinterest_cover(self):
        pinterest = SocialAccount.objects.create(
            workspace=self.ws,
            platform="pinterest",
            account_platform_id="pin-1",
            account_name="Pins",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.client.post(
            self._url("save_post"),
            {
                "action": "save_draft",
                "caption": "pin",
                "selected_accounts": str(pinterest.id),
                f"pin_board_id_{pinterest.id}": "board-1",
                f"pin_cover_image_asset_id_{pinterest.id}": str(self.foreign.id),
            },
        )
        row = PlatformPost.objects.get(post__workspace=self.ws, social_account=pinterest)
        self.assertIsNone(row.platform_extra["cover_image_asset_id"])

    def _thumbnail_uploads(self, asset):
        """Dispatch a video whose stored thumbnail id is ``asset``; returns the bodies sent to thumbnails/set."""
        video = MediaAsset(
            organization=self.org,
            workspace=self.ws,
            filename="v.mp4",
            media_type=MediaAsset.MediaType.VIDEO,
            mime_type="video/mp4",
        )
        video.file.save("v.mp4", ContentFile(b"\x00\x00\x00\x18ftypmp42"), save=False)
        video.save()
        post = Post.objects.create(workspace=self.ws, author=self.manager, caption="video")
        PostMedia.objects.create(post=post, media_asset=video, position=0)
        row = PlatformPost.objects.create(
            post=post,
            social_account=self.youtube,
            status="scheduled",
            platform_extra={"privacy_status": "private", "thumbnail_asset_id": str(asset.id)},
        )
        uploads = []

        def fake_request(provider, method, url, **kwargs):
            if url.endswith("/thumbnails/set"):
                data = kwargs.get("data")
                uploads.append(data.read() if hasattr(data, "read") else data)
            if url.endswith("/videos"):
                return _FakeResponse(headers={"Location": "https://upload.invalid/session"})
            if method == "PUT":
                return _FakeResponse(body={"id": "video-1"})
            return _FakeResponse()

        row = PlatformPost.objects.select_related("post", "social_account__workspace").get(pk=row.pk)
        with mock.patch("providers.base.SocialProvider._request", fake_request):
            PublishEngine()._dispatch_to_provider(row)
        return uploads

    def test_publisher_ignores_a_stored_thumbnail_of_another_tenant(self):
        self.assertEqual(self._thumbnail_uploads(self.foreign), [])

    def test_publisher_uploads_the_workspaces_own_thumbnail(self):
        uploads = self._thumbnail_uploads(self.own)
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0], b"\xff\xd8\xff\xe0own.jpg")
