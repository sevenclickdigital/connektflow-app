"""The card a calendar chip opens."""

from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost
from apps.composer.tests.test_account_scope import AccountScopeTestsBase
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import APIError


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


@override_settings(LANGUAGE_CODE="en-us")
class EditPublishedTextTests(AccountScopeTestsBase):
    """Changing the text of a post that is live on Facebook, from its card."""

    def setUp(self):
        super().setUp()
        self.facebook = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="page-1",
            account_name="My Page",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.live = PlatformPost.objects.create(
            post=self.post,
            social_account=self.facebook,
            status=PlatformPost.Status.PUBLISHED,
            platform_post_id="777",
            published_at=timezone.now(),
        )

    def _edit(self, pp, text="New text", workspace=None):
        return self.client.post(
            reverse(
                "calendar:post_card_edit_text",
                kwargs={"workspace_id": (workspace or self.workspace).id, "platform_post_id": pp.id},
            ),
            {"text": text},
        )

    def _card(self, pp):
        url = reverse("calendar:post_card", kwargs={"workspace_id": self.workspace.id, "platform_post_id": pp.id})
        return self.client.get(url).content.decode()

    def test_the_card_offers_the_edit_only_where_the_network_edits(self):
        edit_url = reverse(
            "calendar:post_card_edit_text", kwargs={"workspace_id": self.workspace.id, "platform_post_id": self.live.id}
        )
        self.assertIn(edit_url, self._card(self.live))
        self.assertIn("https://www.facebook.com/777", self._card(self.live))
        # YouTube publishes no text this can change; a draft is not live.
        self.assertNotIn("/text/", self._card(self.yt_pp))
        draft = PlatformPost.objects.create(post=self.post, social_account=self.facebook, format="reel")
        self.assertNotIn("/text/", self._card(draft))

    @patch("apps.publisher.engine.edit_on_network")
    def test_the_new_text_goes_to_the_network_and_is_kept(self, edit_on_network):
        response = self._edit(self.live, "  New text ")

        self.assertEqual(response.status_code, 200)
        edit_on_network.assert_called_once()
        self.assertEqual(edit_on_network.call_args.args[1], "New text")
        self.live.refresh_from_db()
        self.assertEqual(self.live.platform_specific_caption, "New text")
        self.assertIn("New text", response.content.decode())
        self.assertIn("showToast", response.headers["HX-Trigger"])

    @patch("apps.publisher.engine.edit_on_network", side_effect=APIError("facebook API error 400", status_code=400))
    def test_a_refusal_from_the_network_keeps_the_text_and_says_why(self, _edit_on_network):
        response = self._edit(self.live)

        self.assertEqual(response.status_code, 422)
        self.assertIn("refused to change this post", response.content.decode())
        self.live.refresh_from_db()
        self.assertIsNone(self.live.platform_specific_caption)

    @patch("apps.publisher.engine.edit_on_network")
    def test_what_cannot_be_edited_never_reaches_the_network(self, edit_on_network):
        self.assertEqual(self._edit(self.yt_pp).status_code, 422)
        self.assertEqual(self._edit(self.live, "   ").status_code, 422)
        edit_on_network.assert_not_called()

    @patch("apps.publisher.engine.edit_on_network")
    def test_a_post_imported_from_the_page_is_not_editable(self, edit_on_network):
        PlatformPost.objects.filter(pk=self.live.pk).update(platform_extra={"imported_from_platform": True})
        self.live.refresh_from_db()

        self.assertEqual(self._edit(self.live).status_code, 422)
        self.assertNotIn("/text/", self._card(self.live))
        edit_on_network.assert_not_called()

    @patch("apps.publisher.engine.edit_on_network")
    def test_another_workspaces_post_is_not_found(self, edit_on_network):
        other = Workspace.objects.create(organization=self.org, name="Other")
        WorkspaceMembership.objects.create(
            user=self.user, workspace=other, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )

        self.assertEqual(self._edit(self.live, workspace=other).status_code, 404)
        edit_on_network.assert_not_called()

    @patch("apps.publisher.engine.edit_on_network")
    def test_a_viewer_and_someone_elses_post_are_refused(self, edit_on_network):
        viewer = User.objects.create_user(email="viewer@example.com", password="x", tos_accepted_at=timezone.now())
        WorkspaceMembership.objects.create(
            user=viewer, workspace=self.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )
        self.client.force_login(viewer)
        self.assertEqual(self._edit(self.live).status_code, 403)

        # A contributor creates posts but may only change their own.
        contributor = User.objects.create_user(
            email="contrib@example.com", password="x", tos_accepted_at=timezone.now()
        )
        WorkspaceMembership.objects.create(
            user=contributor, workspace=self.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.CONTRIBUTOR
        )
        self.client.force_login(contributor)
        self.assertEqual(self._edit(self.live).status_code, 403)
        edit_on_network.assert_not_called()
