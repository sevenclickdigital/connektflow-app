"""Tests for client portal magic-link single-use enforcement."""

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.client_portal.services import consume_magic_link, issue_magic_link_token, peek_magic_link
from apps.composer.models import PlatformPost, Post
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class MagicLinkTestBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
        self.client_user = User.objects.create_user(
            email="client@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        WorkspaceMembership.objects.create(
            user=self.client_user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.CLIENT,
        )
        self.token, self.raw_token = issue_magic_link_token(self.client_user, self.workspace)

    def _expire(self, token):
        token.expires_at = timezone.now() - timedelta(days=1)
        token.save(update_fields=["expires_at"])


class PeekMagicLinkTests(MagicLinkTestBase):
    def test_peek_returns_token_for_fresh_token(self):
        self.assertEqual(peek_magic_link(self.raw_token), self.token)

    def test_peek_does_not_consume_token(self):
        peek_magic_link(self.raw_token)
        self.token.refresh_from_db()
        self.assertFalse(self.token.is_consumed)

    def test_peek_returns_none_for_expired_token(self):
        self._expire(self.token)
        self.assertIsNone(peek_magic_link(self.raw_token))

    def test_peek_returns_none_for_consumed_token(self):
        self.token.is_consumed = True
        self.token.save(update_fields=["is_consumed"])
        self.assertIsNone(peek_magic_link(self.raw_token))

    def test_peek_returns_none_for_unknown_token(self):
        self.assertIsNone(peek_magic_link("does-not-exist"))


class ConsumeMagicLinkTests(MagicLinkTestBase):
    def test_consume_first_use_succeeds_and_marks_consumed(self):
        user, workspace, is_valid = consume_magic_link(self.raw_token)
        self.assertTrue(is_valid)
        self.assertEqual(user, self.client_user)
        self.assertEqual(workspace, self.workspace)
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_consumed)
        self.assertIsNotNone(self.token.last_used_at)

    def test_consume_second_use_is_rejected(self):
        consume_magic_link(self.raw_token)
        self.assertEqual(consume_magic_link(self.raw_token), (None, None, False))

    def test_consume_expired_token_is_rejected(self):
        self._expire(self.token)
        self.assertEqual(consume_magic_link(self.raw_token), (None, None, False))

    def test_consume_unknown_token_is_rejected(self):
        self.assertEqual(consume_magic_link("does-not-exist"), (None, None, False))


class MagicLinkEntryViewTests(MagicLinkTestBase):
    def _entry_url(self, token=None):
        return reverse("client_portal:magic_link_entry", kwargs={"token": token or self.raw_token})

    def test_get_renders_confirmation_without_consuming(self):
        response = self.client.get(self._entry_url())
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "client_portal/magic_link_confirm.html")
        self.token.refresh_from_db()
        self.assertFalse(self.token.is_consumed)

    def test_get_with_consumed_token_redirects_to_expired(self):
        self.token.is_consumed = True
        self.token.save(update_fields=["is_consumed"])
        response = self.client.get(self._entry_url())
        self.assertRedirects(response, reverse("client_portal:magic_link_expired"))

    def test_post_consumes_token_and_starts_session(self):
        response = self.client.post(self._entry_url())
        self.assertRedirects(
            response,
            reverse("client_portal:dashboard"),
            fetch_redirect_response=False,
        )
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_consumed)
        self.assertTrue(self.client.session.get("is_portal_session"))

    def test_post_twice_rejects_the_reused_token(self):
        self.client.post(self._entry_url())
        # A fresh client simulates the link being replayed by someone else.
        replay = Client()
        response = replay.post(self._entry_url())
        self.assertRedirects(response, reverse("client_portal:magic_link_expired"))
        self.assertIsNone(replay.session.get("is_portal_session"))


class PortalSessionRoleChangeTests(MagicLinkTestBase):
    """An open portal session follows the member's current role."""

    def setUp(self):
        super().setUp()
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="linkedin_personal",
            account_platform_id="li-portal",
            account_name="LI",
            connection_status="connected",
        )
        self.pending = self._post("pending_client")
        self.approved = self._post("approved")
        self.client.force_login(self.client_user)
        session = self.client.session
        session["is_portal_session"] = True
        session["portal_workspace_id"] = str(self.workspace.id)
        session.save()

    def _post(self, status):
        post = Post.objects.create(workspace=self.workspace, caption=status)
        PlatformPost.objects.create(post=post, social_account=self.account, status=status)
        return post

    def _status(self, post):
        return PlatformPost.objects.get(post=post).status

    def test_demoted_member_cannot_act_or_read_through_the_portal(self):
        WorkspaceMembership.objects.filter(user=self.client_user, workspace=self.workspace).update(
            workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )
        expired = reverse("client_portal:magic_link_expired")

        for name, post in (
            ("approve", self.pending),
            ("reject", self.pending),
            ("request_changes", self.pending),
            ("request_hold", self.approved),
        ):
            with self.subTest(action=name):
                response = self.client.post(
                    reverse(f"client_portal:{name}", kwargs={"post_id": post.id}), data={"comment": "x"}
                )
                self.assertRedirects(response, expired, fetch_redirect_response=False)
        self.assertRedirects(
            self.client.get(reverse("client_portal:approval_queue")), expired, fetch_redirect_response=False
        )
        self.assertEqual(self._status(self.pending), "pending_client")
        self.assertEqual(self._status(self.approved), "approved")

    def test_client_still_approves(self):
        response = self.client.post(reverse("client_portal:approve", kwargs={"post_id": self.pending.id}))
        self.assertRedirects(response, reverse("client_portal:approval_queue"), fetch_redirect_response=False)
        self.assertNotEqual(self._status(self.pending), "pending_client")
