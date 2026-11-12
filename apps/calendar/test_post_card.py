"""The card a calendar chip opens."""

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost
from apps.composer.tests.test_account_scope import AccountScopeTestsBase
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@override_settings(LANGUAGE_CODE="en-us")
class PostCardTests(AccountScopeTestsBase):
    def setUp(self):
        super().setUp()
        self.instagram = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig-card",
            account_name="Insta Page",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.feed = PlatformPost.objects.create(post=self.post, social_account=self.instagram)
        self.story = PlatformPost.objects.create(post=self.post, social_account=self.instagram, format="story")

    def _card(self, pp, workspace=None):
        return self.client.get(
            reverse(
                "calendar:post_card",
                kwargs={"workspace_id": (workspace or self.workspace).id, "platform_post_id": pp.id},
            )
        )

    def test_the_card_names_the_post_and_offers_its_actions(self):
        body = self._card(self.feed).content.decode()

        self.assertIn("Insta Page", body)
        self.assertIn("Instagram post", body)
        self.assertIn(
            reverse("composer:clone_post", kwargs={"workspace_id": self.workspace.id, "post_id": self.post.id}), body
        )
        self.assertIn(
            reverse(
                "automations:flow_builder", kwargs={"workspace_id": self.workspace.id, "platform_post_id": self.feed.id}
            ),
            body,
        )

    def test_a_story_has_no_automation(self):
        body = self._card(self.story).content.decode()

        self.assertIn("Instagram Stories", body)
        self.assertNotIn("/flow/", body)

    def test_another_workspaces_post_is_not_found(self):
        other = Workspace.objects.create(organization=self.org, name="Other")
        WorkspaceMembership.objects.create(
            user=self.user, workspace=other, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )

        self.assertEqual(self._card(self.feed, workspace=other).status_code, 404)

    def test_a_non_member_is_refused(self):
        outsider = User.objects.create_user(email="out@example.com", password="x", tos_accepted_at=timezone.now())
        self.client.force_login(outsider)

        self.assertIn(self._card(self.feed).status_code, (403, 404))

    def test_a_viewer_gets_no_clone(self):
        viewer = User.objects.create_user(email="viewer@example.com", password="x", tos_accepted_at=timezone.now())
        WorkspaceMembership.objects.create(
            user=viewer, workspace=self.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )
        self.client.force_login(viewer)

        body = self._card(self.feed).content.decode()

        self.assertNotIn("/clone/", body)
