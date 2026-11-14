"""A post going out as several formats of one account: what keeps them apart and together."""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.calendar.models import Queue, QueueEntry
from apps.calendar.services import drop_queue_entry_unless_scheduled
from apps.composer.models import PlatformPost, Post
from apps.composer.services import format_blocker, publish_blocker, require_publishable
from apps.composer.tests.test_account_scope import AccountScopeTestsBase
from apps.media_library.models import MediaAsset
from apps.social_accounts.models import SocialAccount


class FormatsTestsBase(AccountScopeTestsBase):
    def setUp(self):
        super().setUp()
        self.instagram = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig-formats",
            account_name="Instagram",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.feed = PlatformPost.objects.create(post=self.post, social_account=self.instagram)
        self.story = PlatformPost.objects.create(post=self.post, social_account=self.instagram, format="story")


class FormatBlockerTests(FormatsTestsBase):
    def test_a_story_with_two_images_goes_out_as_two_stories(self):
        self._attach(MediaAsset.MediaType.IMAGE, "a.jpg", post=self.post)
        self._attach(MediaAsset.MediaType.IMAGE, "b.jpg", post=self.post)

        require_publishable(self.story)
        require_publishable(self.feed)

    def test_a_story_needs_a_photo_or_a_video(self):
        self.assertEqual(format_blocker("story", []), "A Story needs at least one photo or video.")
        self.assertEqual(format_blocker("story", ["document"]), "A Story needs at least one photo or video.")
        self.assertEqual(format_blocker("story", ["image", "video"]), "")

    def test_the_engine_check_uses_the_ordered_media(self):
        reason = publish_blocker(
            self.instagram,
            media_types={"image"},
            format="reel",
            ordered_media_types=["image"],
        )
        self.assertEqual(reason, "A Reel needs exactly one video.")


class SharedQueueSlotTests(FormatsTestsBase):
    def setUp(self):
        super().setUp()
        self.queue = Queue.objects.create(workspace=self.workspace, name="IG", social_account=self.instagram)
        QueueEntry.objects.create(queue=self.queue, post=self.post, position=1)
        later = timezone.now() + timedelta(days=1)
        PlatformPost.objects.filter(pk__in=[self.feed.pk, self.story.pk]).update(status="scheduled", scheduled_at=later)

    def test_the_slot_stays_while_another_format_is_still_scheduled(self):
        PlatformPost.objects.filter(pk=self.feed.pk).update(status="published", published_at=timezone.now())

        drop_queue_entry_unless_scheduled(self.post.pk, self.instagram.pk)

        self.assertTrue(QueueEntry.objects.filter(post=self.post).exists())

    def test_the_slot_goes_once_no_format_is_scheduled(self):
        PlatformPost.objects.filter(pk__in=[self.feed.pk, self.story.pk]).update(status="draft")

        drop_queue_entry_unless_scheduled(self.post.pk, self.instagram.pk)

        self.assertFalse(QueueEntry.objects.filter(post=self.post).exists())


class DeleteOneFormatTests(FormatsTestsBase):
    def _delete_url(self, **query):
        url = reverse("composer:post_delete", kwargs={"workspace_id": self.workspace.id, "post_id": self.post.id})
        return url + "?" + "&".join(f"{k}={v}" for k, v in query.items())

    def test_platform_post_removes_only_that_format(self):
        response = self.client.post(self._delete_url(platform_post=self.story.pk))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(PlatformPost.objects.filter(pk=self.story.pk).exists())
        self.assertTrue(PlatformPost.objects.filter(pk=self.feed.pk).exists())

    def test_account_removes_every_format_of_that_account(self):
        response = self.client.post(self._delete_url(account=self.instagram.pk))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(self.post.platform_posts.filter(social_account=self.instagram).exists())

    def test_a_platform_post_of_another_post_is_not_found(self):
        other = Post.objects.create(workspace=self.workspace, author=self.user)
        foreign = PlatformPost.objects.create(post=other, social_account=self.instagram)

        response = self.client.post(self._delete_url(platform_post=foreign.pk))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(PlatformPost.objects.filter(pk=foreign.pk).exists())


class DisconnectOrphanTests(FormatsTestsBase):
    def test_a_post_only_on_this_account_goes_even_with_two_formats(self):
        lonely = Post.objects.create(workspace=self.workspace, author=self.user)
        PlatformPost.objects.create(post=lonely, social_account=self.instagram)
        PlatformPost.objects.create(post=lonely, social_account=self.instagram, format="story")

        self.client.post(
            reverse(
                "social_accounts:disconnect",
                kwargs={"workspace_id": self.workspace.id, "account_id": self.instagram.id},
            )
        )

        self.assertFalse(Post.objects.filter(pk=lonely.pk).exists())
        # self.post also targets YouTube and TikTok, so it survives.
        self.assertTrue(Post.objects.filter(pk=self.post.pk).exists())


class PreviewSlidesTests(FormatsTestsBase):
    def test_one_card_per_account_with_a_slide_per_format(self):
        acc = str(self.instagram.id)
        response = self.client.post(
            reverse("composer:preview", kwargs={"workspace_id": self.workspace.id}),
            data={
                "caption": "shared",
                "selected_accounts": acc,
                "selected_targets": f"{acc}:default,{acc}:story,{acc}:reel",
                f"override_caption_{acc}_reel": "Only the Reel",
            },
        )

        body = response.content.decode()
        self.assertEqual(body.count('class="preview-card'), 1)
        self.assertIn("Instagram Stories Preview", body)
        self.assertIn("Instagram Reels Preview", body)
        self.assertIn("Only the Reel", body)


class AutosaveNewPostTests(FormatsTestsBase):
    def _autosave(self, **data):
        return self.client.post(reverse("composer:autosave", kwargs={"workspace_id": self.workspace.id}), data=data)

    def test_an_untouched_composer_creates_no_draft(self):
        before = Post.objects.count()

        response = self._autosave(caption="", title="", selected_accounts="")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Post.objects.count(), before)

    def test_a_composer_with_a_caption_creates_the_draft(self):
        before = Post.objects.count()

        self._autosave(caption="Hello", selected_accounts="")

        self.assertEqual(Post.objects.count(), before + 1)
