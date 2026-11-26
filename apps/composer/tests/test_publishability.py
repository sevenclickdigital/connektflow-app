"""The service layer refuses to schedule a post that is certain to fail.

``create_post`` and ``transition_platform_post`` are what the REST API and the
MCP tools schedule through, so a TikTok post with no video — or a Pinterest pin
with no board, which the API has no way to set — used to be accepted there and
fail at publish time, usually after the post's other platforms went out.
"""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.composer.models import PlatformPost, Post, PostMedia
from apps.composer.services import create_post, publish_blocker, transition_platform_post
from apps.media_library.models import MediaAsset
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

IMAGE = {MediaAsset.MediaType.IMAGE}
VIDEO = {MediaAsset.MediaType.VIDEO}


# The blocker messages are translated, and the project's default language
# is pt-br. Pin English so these assert on the rule, not the catalogue.
@override_settings(LANGUAGE_CODE="en-us")
class PublishBlockerTest(TestCase):
    def _account(self, platform):
        return SocialAccount(platform=platform)

    def test_video_only_platforms_need_a_video(self):
        for platform, name in (("tiktok", "TikTok"), ("youtube", "YouTube")):
            with self.subTest(platform=platform):
                for media in (set(), IMAGE):
                    reason = publish_blocker(self._account(platform), media_types=media)
                    assert reason.startswith(f"{name} can only publish videos")
                assert publish_blocker(self._account(platform), media_types=VIDEO) == ""

    def test_instagram_and_pinterest_need_some_media(self):
        board = {"board_id": "b-1"}
        for platform in ("instagram", "instagram_login", "pinterest"):
            with self.subTest(platform=platform):
                reason = publish_blocker(self._account(platform), media_types=set(), platform_extra=board)
                assert "needs an image or a video" in reason
                for media in (IMAGE, VIDEO):
                    assert publish_blocker(self._account(platform), media_types=media, platform_extra=board) == ""

    def test_pinterest_needs_a_board(self):
        assert "board" in publish_blocker(self._account("pinterest"), media_types=IMAGE)

    def test_a_facebook_feed_post_takes_fifteen_photos(self):
        facebook = self._account("facebook")
        assert publish_blocker(facebook, media_types=IMAGE, image_count=15) == ""
        assert "up to 15 photos" in publish_blocker(facebook, media_types=IMAGE, image_count=16)
        # Stories publish one by one, and a platform without the limit is untouched.
        assert publish_blocker(facebook, media_types=IMAGE, image_count=16, format="story") == ""
        assert publish_blocker(self._account("threads"), media_types=IMAGE, image_count=16) == ""

    def test_text_platforms_are_not_blocked(self):
        for platform in ("facebook", "linkedin_personal", "bluesky", "threads"):
            with self.subTest(platform=platform):
                assert publish_blocker(self._account(platform), media_types=set()) == ""


@override_settings(LANGUAGE_CODE="en-us")
class ServiceSchedulingTest(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.tiktok = self._account("tiktok")
        self.when = timezone.now() + timedelta(hours=1)

    def _account(self, platform):
        return SocialAccount.objects.create(
            workspace=self.workspace,
            platform=platform,
            account_platform_id=f"{platform}-1",
            account_name=platform,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def _asset(self, media_type):
        return MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            file=f"test/{media_type}",
            filename=media_type,
            media_type=media_type,
            mime_type="video/mp4" if media_type == MediaAsset.MediaType.VIDEO else "image/jpeg",
        )

    def _create(self, account, *, status="scheduled", media=()):
        return create_post(
            workspace=self.workspace,
            social_account=account,
            caption="hi",
            media_asset_ids=[asset.id for asset in media],
            scheduled_at=self.when if status == "scheduled" else None,
            status=status,
        )

    def test_scheduling_tiktok_with_only_an_image_is_refused_and_writes_nothing(self):
        with self.assertRaisesMessage(ValueError, "TikTok can only publish videos"):
            self._create(self.tiktok, media=[self._asset(MediaAsset.MediaType.IMAGE)])

        assert not Post.objects.exists()

    def test_scheduling_sixteen_photos_to_facebook_is_refused(self):
        facebook = self._account("facebook")
        photos = [self._asset(MediaAsset.MediaType.IMAGE) for _ in range(16)]

        with self.assertRaisesMessage(ValueError, "up to 15 photos"):
            self._create(facebook, media=photos)
        assert Post.objects.count() == 0

        self._create(facebook, media=photos[:15])
        assert Post.objects.count() == 1

    def test_scheduling_tiktok_with_a_video_is_allowed(self):
        post = self._create(self.tiktok, media=[self._asset(MediaAsset.MediaType.VIDEO)])

        assert post.platform_posts.get().status == PlatformPost.Status.SCHEDULED

    def test_a_draft_may_wait_for_its_video(self):
        post = self._create(self.tiktok, status="draft")

        assert post.platform_posts.get().status == PlatformPost.Status.DRAFT

    def test_scheduling_a_new_pinterest_pin_is_refused(self):
        """A post created through the API has no board, and the API can't set one."""
        with self.assertRaisesMessage(ValueError, "Pinterest needs a board"):
            self._create(self._account("pinterest"), media=[self._asset(MediaAsset.MediaType.IMAGE)])

    def test_other_platforms_schedule_without_media(self):
        post = self._create(self._account("linkedin_personal"))

        assert post.platform_posts.get().status == PlatformPost.Status.SCHEDULED

    def test_scheduling_a_draft_without_a_video_is_refused(self):
        pp = self._create(self.tiktok, status="draft").platform_posts.get()

        with self.assertRaisesMessage(ValueError, "TikTok can only publish videos"):
            transition_platform_post(pp, "scheduled", scheduled_at=self.when)

        pp.refresh_from_db()
        assert pp.status == PlatformPost.Status.DRAFT

    def test_a_pinterest_draft_with_a_board_can_be_scheduled(self):
        """The composer route: draft via the API, board picked in the composer."""
        image = self._asset(MediaAsset.MediaType.IMAGE)
        pp = self._create(self._account("pinterest"), status="draft", media=[image]).platform_posts.get()
        pp.platform_extra = {"board_id": "board-1"}
        pp.save(update_fields=["platform_extra"])

        transition_platform_post(pp, "scheduled", scheduled_at=self.when)

        pp.refresh_from_db()
        assert pp.status == PlatformPost.Status.SCHEDULED

    def test_a_video_attached_after_the_draft_counts(self):
        pp = self._create(self.tiktok, status="draft").platform_posts.get()
        PostMedia.objects.create(post=pp.post, media_asset=self._asset(MediaAsset.MediaType.VIDEO))

        transition_platform_post(pp, "scheduled", scheduled_at=self.when)

        pp.refresh_from_db()
        assert pp.status == PlatformPost.Status.SCHEDULED

    def test_a_video_with_no_stored_file_does_not_count(self):
        """The publisher skips attachments with no file, so scheduling must too."""
        video = self._asset(MediaAsset.MediaType.VIDEO)
        MediaAsset.objects.filter(pk=video.pk).update(file="")
        video.refresh_from_db()

        with self.assertRaisesMessage(ValueError, "TikTok can only publish videos"):
            self._create(self.tiktok, media=[video])

    def test_scheduling_an_instagram_post_with_no_media_is_refused(self):
        with self.assertRaisesMessage(ValueError, "Instagram needs an image or a video"):
            self._create(self._account("instagram"))


@override_settings(LANGUAGE_CODE="en-us")
class TextBlockerTest(TestCase):
    """A post's text: nothing at all, too long for the platform, a DEV.to article without a title."""

    def _account(self, platform):
        return SocialAccount(platform=platform)

    def test_a_post_with_neither_text_nor_media_is_refused(self):
        reason = publish_blocker(self._account("facebook"), media_types=set(), caption="  ")
        assert reason.startswith("There is nothing to publish on Facebook")

    def test_media_alone_is_enough(self):
        assert publish_blocker(self._account("facebook"), media_types=IMAGE, caption="") == ""

    def test_a_text_over_the_platform_limit_is_refused(self):
        reason = publish_blocker(self._account("x"), media_types=set(), caption="a" * 281)
        assert reason.startswith("The text has 281 characters and X takes up to 280")
        assert publish_blocker(self._account("x"), media_types=set(), caption="a" * 280) == ""

    def test_the_length_check_can_be_left_out(self):
        assert publish_blocker(self._account("x"), media_types=set(), caption="a" * 281, check_length=False) == ""

    def test_a_story_goes_out_without_its_text(self):
        account = self._account("facebook")
        assert publish_blocker(account, media_types=IMAGE, caption="a" * 70000, format="story") == ""

    def test_a_devto_article_needs_a_title(self):
        account = self._account("devto")
        assert publish_blocker(account, media_types=set(), caption="body", title=" ").startswith("DEV.to needs a title")
        assert publish_blocker(account, media_types=set(), caption="body", title="Title") == ""

    def test_without_the_text_nothing_is_checked(self):
        assert publish_blocker(self._account("facebook"), media_types=set()) == ""


@override_settings(LANGUAGE_CODE="en-us")
class TextSchedulingTest(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=org, name="WS")
        self.facebook = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="fb-1",
            account_name="Page",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def test_scheduling_an_empty_post_is_refused(self):
        with self.assertRaisesMessage(ValueError, "There is nothing to publish on Facebook"):
            create_post(
                workspace=self.workspace,
                social_account=self.facebook,
                caption="",
                scheduled_at=timezone.now() + timedelta(hours=1),
                status="scheduled",
            )
        assert not Post.objects.exists()

    def test_an_empty_draft_may_wait_for_its_text(self):
        post = create_post(workspace=self.workspace, social_account=self.facebook, caption="", status="draft")
        assert post.platform_posts.get().status == PlatformPost.Status.DRAFT
