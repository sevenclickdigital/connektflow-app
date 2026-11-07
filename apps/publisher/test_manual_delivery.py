"""Notified publishing: the post is handed to a person instead of the platform."""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.notifications.models import EventType, Notification
from apps.organizations.models import Organization
from apps.publisher.engine import PublishEngine
from apps.publisher.manual_delivery import deliver_by_notification
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


def _user(email):
    return User.objects.create_user(email=email, password="pw-12345678", tos_accepted_at=timezone.now())


@override_settings(LANGUAGE_CODE="en-us", APP_URL="https://studio.example.com")
class ManualDeliveryTest(TestCase):
    def setUp(self):
        self.author = _user("author@example.com")
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig-1",
            account_name="Shop",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.post = Post.objects.create(workspace=self.workspace, caption="Hello", author=self.author)
        self.platform_post = PlatformPost.objects.create(
            post=self.post,
            social_account=self.account,
            status=PlatformPost.Status.SCHEDULED,
            platform_extra={"post_type": "story", "delivery": "notify"},
        )

    def test_the_author_is_notified_with_a_link_to_the_manual_publish_page(self):
        result = deliver_by_notification(self.platform_post)

        self.assertTrue(result["success"])
        notification = Notification.objects.get(user=self.author)
        self.assertEqual(notification.event_type, EventType.MANUAL_PUBLISH_DUE)
        self.assertIn("Shop", notification.title)
        self.assertEqual(
            notification.data["action_url"],
            "https://studio.example.com"
            + reverse(
                "composer:manual_publish",
                kwargs={
                    "workspace_id": self.workspace.id,
                    "post_id": self.post.id,
                    "platform_post_id": self.platform_post.id,
                },
            ),
        )

    def test_without_an_author_the_members_who_can_post_are_notified(self):
        self.post.author = None
        self.post.save(update_fields=["author"])
        editor, viewer = _user("editor@example.com"), _user("viewer@example.com")
        WorkspaceMembership.objects.create(user=editor, workspace=self.workspace, workspace_role="editor")
        WorkspaceMembership.objects.create(user=viewer, workspace=self.workspace, workspace_role="viewer")

        deliver_by_notification(self.platform_post)

        self.assertEqual(list(Notification.objects.values_list("user__email", flat=True)), ["editor@example.com"])

    def test_with_no_one_to_notify_it_raises_so_the_engine_retries(self):
        self.post.author = None
        self.post.save(update_fields=["author"])

        with self.assertRaises(RuntimeError):
            deliver_by_notification(self.platform_post)

    def test_the_engine_notifies_instead_of_calling_the_platform(self):
        with patch("apps.publisher.engine.get_provider") as get_provider:
            PublishEngine()._publish_platform_post(self.platform_post)

        get_provider.assert_not_called()
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.status, PlatformPost.Status.PUBLISHED)
        self.assertEqual(self.platform_post.platform_post_id, "")
        self.assertIn("notified_at", self.platform_post.platform_extra)
        self.assertTrue(self.platform_post.delivered_by_notification)
        self.assertTrue(Notification.objects.filter(user=self.author).exists())


@override_settings(LANGUAGE_CODE="en-us")
class ManualPublishPageTest(TestCase):
    def setUp(self):
        self.user = _user("member@example.com")
        self.org = Organization.objects.create(name="Org")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role="member")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        WorkspaceMembership.objects.create(user=self.user, workspace=self.workspace, workspace_role="editor")
        account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="page-1",
            account_name="Page",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.post = Post.objects.create(workspace=self.workspace, caption="Copy me")
        self.platform_post = PlatformPost.objects.create(
            post=self.post,
            social_account=account,
            status=PlatformPost.Status.PUBLISHED,
            platform_extra={"post_type": "story", "delivery": "notify"},
        )
        self.client.force_login(self.user)

    def _url(self, workspace=None, platform_post=None):
        return reverse(
            "composer:manual_publish",
            kwargs={
                "workspace_id": (workspace or self.workspace).id,
                "post_id": self.post.id,
                "platform_post_id": (platform_post or self.platform_post).id,
            },
        )

    def test_shows_the_caption_to_copy(self):
        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Copy me")
        self.assertContains(response, "published it")

    def test_marking_it_published_is_recorded_once(self):
        self.client.post(self._url())
        self.platform_post.refresh_from_db()
        first = self.platform_post.platform_extra["manual_published_at"]

        self.client.post(self._url())

        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.platform_extra["manual_published_at"], first)

    def test_a_channel_published_through_the_api_has_no_manual_page(self):
        self.platform_post.platform_extra = {"post_type": "story"}
        self.platform_post.save(update_fields=["platform_extra"])

        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_another_workspace_cannot_open_it(self):
        other = Workspace.objects.create(organization=self.org, name="Other")
        WorkspaceMembership.objects.create(user=self.user, workspace=other, workspace_role="editor")

        self.assertEqual(self.client.get(self._url(workspace=other)).status_code, 404)
