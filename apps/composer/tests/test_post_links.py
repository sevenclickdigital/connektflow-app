"""Where "View on <network>" leads for a published post."""

from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost
from apps.composer.services import post_permalink, post_view_url
from apps.composer.tests.test_account_scope import AccountScopeTestsBase
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import APIError


@override_settings(LANGUAGE_CODE="en-us")
class PostLinkTests(AccountScopeTestsBase):
    def setUp(self):
        super().setUp()
        self.instagram = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig-1",
            account_name="Insta",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.ig_pp = PlatformPost.objects.create(
            post=self.post,
            social_account=self.instagram,
            status=PlatformPost.Status.PUBLISHED,
            platform_post_id="1789",
            published_at=timezone.now(),
        )
        self.open_url = reverse(
            "composer:post_open", kwargs={"workspace_id": self.workspace.id, "platform_post_id": self.ig_pp.id}
        )

    def test_a_link_built_from_the_id_needs_no_lookup(self):
        self.assertEqual(post_view_url(self.yt_pp), "https://www.youtube.com/watch?v=yt-video-1")

    def test_instagram_is_opened_through_the_lookup(self):
        self.assertEqual(post_permalink(self.ig_pp), "")
        self.assertEqual(post_view_url(self.ig_pp), self.open_url)

    def test_an_imported_post_keeps_the_link_it_came_with_when_it_is_the_networks(self):
        self.ig_pp.platform_extra = {"permalink": "https://www.instagram.com/p/abc/"}
        self.assertEqual(post_view_url(self.ig_pp), "https://www.instagram.com/p/abc/")

        for stored in ("https://evil.example/p/abc/", "http://www.instagram.com/p/abc/", "javascript:alert(1)"):
            self.ig_pp.platform_extra = {"permalink": stored}
            self.assertEqual(post_view_url(self.ig_pp), self.open_url)

    def test_nothing_opens_what_is_not_live_or_has_no_link(self):
        self.assertEqual(post_view_url(self.tt_pp), "")
        tiktok_live = PlatformPost(
            post=self.post, social_account=self.tiktok, status=PlatformPost.Status.PUBLISHED, platform_post_id="t-1"
        )
        self.assertEqual(post_view_url(tiktok_live), "")

    @patch("apps.publisher.engine.permalink_on_network", return_value="https://www.instagram.com/p/xyz/")
    def test_opening_redirects_to_the_link_the_network_gave(self, _lookup):
        response = self.client.get(self.open_url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://www.instagram.com/p/xyz/")

    def test_a_link_that_is_not_the_networks_or_a_refusal_lands_on_the_calendar(self):
        calendar = reverse("calendar:calendar", kwargs={"workspace_id": self.workspace.id})
        with patch("apps.publisher.engine.permalink_on_network", return_value="https://evil.example/x"):
            self.assertRedirects(self.client.get(self.open_url), calendar, fetch_redirect_response=False)
        with patch("apps.publisher.engine.permalink_on_network", side_effect=APIError("boom", status_code=400)):
            self.assertRedirects(self.client.get(self.open_url), calendar, fetch_redirect_response=False)

    @patch("apps.publisher.engine.permalink_on_network")
    def test_another_workspace_and_a_non_member_cannot_open_it(self, lookup):
        other = Workspace.objects.create(organization=self.org, name="Other")
        WorkspaceMembership.objects.create(
            user=self.user, workspace=other, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        foreign = reverse("composer:post_open", kwargs={"workspace_id": other.id, "platform_post_id": self.ig_pp.id})
        self.assertEqual(self.client.get(foreign).status_code, 404)

        outsider = User.objects.create_user(email="out@example.com", password="x", tos_accepted_at=timezone.now())
        self.client.force_login(outsider)
        self.assertIn(self.client.get(self.open_url).status_code, (403, 404))
        lookup.assert_not_called()
