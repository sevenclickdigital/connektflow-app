"""Cropping an attached image from the composer."""

import io

from django.core.files.base import ContentFile
from django.urls import reverse
from PIL import Image

from apps.composer.models import PostMedia
from apps.composer.tests.test_account_scope import AccountScopeTestsBase
from apps.media_library.models import MediaAsset
from apps.workspaces.models import Workspace


def _png(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "purple").save(buffer, format="PNG")
    return ContentFile(buffer.getvalue(), name="logo.png")


class CropMediaTestsBase(AccountScopeTestsBase):
    def setUp(self):
        super().setUp()
        self.asset = MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            uploaded_by=self.user,
            file=_png(400, 300),
            filename="logo.png",
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/png",
            width=400,
            height=300,
        )
        self.attachment = PostMedia.objects.create(post=self.post, media_asset=self.asset, position=1)
        # The base post has published children; make it editable again.
        self.post.platform_posts.update(status="draft")
        self.url = reverse(
            "composer:crop_media",
            kwargs={"workspace_id": self.workspace.id, "post_id": self.post.id, "media_id": self.attachment.id},
        )


class CropMediaTests(CropMediaTestsBase):
    def test_the_attachment_gets_a_cropped_copy_and_the_original_is_kept(self):
        response = self.client.post(self.url, data={"x": 10, "y": 20, "width": 160, "height": 200})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Trigger"], "previewUpdate")
        self.attachment.refresh_from_db()
        cropped = self.attachment.media_asset
        self.assertNotEqual(cropped.pk, self.asset.pk)
        self.assertEqual((cropped.width, cropped.height), (160, 200))
        with Image.open(cropped.file) as img:
            self.assertEqual(img.size, (160, 200))
        self.asset.refresh_from_db()
        with Image.open(self.asset.file) as img:
            self.assertEqual(img.size, (400, 300))

    def test_a_box_outside_the_image_is_refused(self):
        response = self.client.post(self.url, data={"x": 300, "y": 0, "width": 200, "height": 100})

        self.assertEqual(response.status_code, 400)
        self.attachment.refresh_from_db()
        self.assertEqual(self.attachment.media_asset_id, self.asset.pk)

    def test_another_workspace_cannot_crop_this_attachment(self):
        other = Workspace.objects.create(organization=self.org, name="Other")
        url = reverse(
            "composer:crop_media",
            kwargs={"workspace_id": other.id, "post_id": self.post.id, "media_id": self.attachment.id},
        )

        response = self.client.post(url, data={"x": 0, "y": 0, "width": 10, "height": 10})

        self.assertIn(response.status_code, (403, 404))

    def test_a_pending_upload_is_replaced_in_place(self):
        second = MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            uploaded_by=self.user,
            file=_png(100, 100),
            filename="b.png",
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/png",
        )
        session = self.client.session
        session[f"pending_media_{self.workspace.id}"] = [str(self.asset.id), str(second.id)]
        session.save()

        response = self.client.post(
            reverse(
                "composer:crop_pending_media",
                kwargs={"workspace_id": self.workspace.id, "asset_id": self.asset.id},
            ),
            data={"x": 0, "y": 0, "width": 300, "height": 300},
        )

        self.assertEqual(response.status_code, 200)
        pending = self.client.session[f"pending_media_{self.workspace.id}"]
        self.assertEqual(len(pending), 2)
        self.assertNotEqual(pending[0], str(self.asset.id))
        self.assertEqual(pending[1], str(second.id))

    def test_a_pending_asset_not_in_this_session_is_not_found(self):
        response = self.client.post(
            reverse(
                "composer:crop_pending_media",
                kwargs={"workspace_id": self.workspace.id, "asset_id": self.asset.id},
            ),
            data={"x": 0, "y": 0, "width": 10, "height": 10},
        )

        self.assertEqual(response.status_code, 404)

    def test_the_cropped_copy_is_queued_for_processing_and_its_card_waits_quietly(self):
        from background_task.models import Task

        self.client.post(self.url, data={"x": 0, "y": 0, "width": 100, "height": 100})
        self.attachment.refresh_from_db()
        cropped = self.attachment.media_asset

        self.assertTrue(
            Task.objects.filter(
                task_name__endswith="process_media_asset", task_params__contains=str(cropped.id)
            ).exists()
        )
        # Until that runs the library card keeps polling; a still-pending answer
        # must not replace the card.
        status_url = reverse(
            "media_library:processing_status",
            kwargs={"workspace_id": self.workspace.id, "asset_id": cropped.id},
        )
        response = self.client.get(status_url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)

    def test_a_pending_thumbnail_the_autosave_already_attached_is_cropped_on_the_post(self):
        # The autosave moved it onto the post and emptied the session; the
        # thumbnail on screen still carries its pending-media URL.
        response = self.client.post(
            reverse(
                "composer:crop_pending_media",
                kwargs={"workspace_id": self.workspace.id, "asset_id": self.asset.id},
            ),
            data={"x": 0, "y": 0, "width": 200, "height": 200, "_autosave_post_id": str(self.post.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.attachment.refresh_from_db()
        self.assertNotEqual(self.attachment.media_asset_id, self.asset.pk)
        self.assertEqual(self.attachment.media_asset.width, 200)

    def test_a_pending_thumbnail_the_autosave_already_attached_is_removed_from_the_post(self):
        response = self.client.post(
            reverse(
                "composer:remove_pending_media",
                kwargs={"workspace_id": self.workspace.id, "asset_id": self.asset.id},
            ),
            data={"_autosave_post_id": str(self.post.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PostMedia.objects.filter(pk=self.attachment.pk).exists())
        # Only the attachment goes; the library keeps the image.
        self.assertTrue(MediaAsset.objects.filter(pk=self.asset.pk).exists())

    def test_another_workspaces_post_id_does_not_resolve_the_attachment(self):
        other = Workspace.objects.create(organization=self.org, name="Other")
        response = self.client.post(
            reverse(
                "composer:crop_pending_media",
                kwargs={"workspace_id": other.id, "asset_id": self.asset.id},
            ),
            data={"x": 0, "y": 0, "width": 10, "height": 10, "_autosave_post_id": str(self.post.id)},
        )

        self.assertIn(response.status_code, (403, 404))


class FormatCropTests(CropMediaTestsBase):
    """A crop made for one format: the Story gets 9:16, the feed keeps the original."""

    def setUp(self):
        super().setUp()
        from apps.composer.models import PlatformPost
        from apps.social_accounts.models import SocialAccount

        self.instagram = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig-crop",
            account_name="Instagram",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.feed = PlatformPost.objects.create(post=self.post, social_account=self.instagram)
        self.story = PlatformPost.objects.create(post=self.post, social_account=self.instagram, format="story")
        self.format_url = reverse(
            "composer:crop_format_media", kwargs={"workspace_id": self.workspace.id, "post_id": self.post.id}
        )

    def _crop_story(self, **box):
        return self.client.post(
            self.format_url,
            data={
                "asset_id": str(self.asset.id),
                "target": f"{self.instagram.id}:story",
                **({"x": 100, "y": 0, "width": 169, "height": 300} | box),
            },
        )

    def test_only_the_story_publishes_the_crop(self):
        from apps.composer.services import apply_format_media

        response = self._crop_story()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"], str(self.asset.id))
        self.assertEqual((payload["asset"]["width"], payload["asset"]["height"]), (169, 300))
        # The post's attachment is untouched...
        self.attachment.refresh_from_db()
        self.assertEqual(self.attachment.media_asset_id, self.asset.pk)
        # ...the Story swaps in its own copy, the feed doesn't.
        self.story.refresh_from_db()
        self.feed.refresh_from_db()
        story_media = apply_format_media(self.story, list(self.post.media_attachments.select_related("media_asset")))
        feed_media = apply_format_media(self.feed, list(self.post.media_attachments.select_related("media_asset")))
        self.assertEqual(str(story_media[0].media_asset_id), payload["asset"]["id"])
        self.assertEqual(feed_media[0].media_asset_id, self.asset.pk)

    def test_cropping_the_same_image_again_replaces_the_earlier_crop(self):
        first = self._crop_story().json()["asset"]["id"]
        second = self._crop_story(x=0).json()["asset"]["id"]

        self.story.refresh_from_db()
        self.assertEqual(self.story.platform_specific_media, [second])
        self.assertNotEqual(first, second)

    def test_the_format_crop_is_kept_by_the_orphan_sweep(self):
        from apps.media_library.services import _json_referenced_asset_ids

        cropped_id = self._crop_story().json()["asset"]["id"]

        self.assertIn(cropped_id, {str(i) for i in _json_referenced_asset_ids()})

    def test_a_format_the_post_does_not_have_is_not_found(self):
        response = self.client.post(
            self.format_url,
            data={
                "asset_id": str(self.asset.id),
                "target": f"{self.instagram.id}:reel",
                "x": 0,
                "y": 0,
                "width": 10,
                "height": 10,
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_another_workspace_cannot_crop_for_a_format(self):
        other = Workspace.objects.create(organization=self.org, name="Other")
        url = reverse("composer:crop_format_media", kwargs={"workspace_id": other.id, "post_id": self.post.id})

        response = self.client.post(
            url,
            data={
                "asset_id": str(self.asset.id),
                "target": f"{self.instagram.id}:story",
                "x": 0,
                "y": 0,
                "width": 10,
                "height": 10,
            },
        )

        self.assertIn(response.status_code, (403, 404))

    def test_the_story_preview_slide_shows_the_crop(self):
        cropped_url = self._crop_story().json()["asset"]["url"]
        acc = str(self.instagram.id)

        response = self.client.post(
            reverse("composer:preview", kwargs={"workspace_id": self.workspace.id}),
            data={
                "caption": "x",
                "selected_accounts": acc,
                "selected_targets": f"{acc}:default,{acc}:story",
                "_autosave_post_id": str(self.post.id),
            },
        )

        body = response.content.decode()
        # The Story slide shows the crop; the feed slide keeps the original.
        self.assertEqual(body.count(cropped_url), 1)
        self.assertEqual(body.count(self.asset.file.url), 1)
