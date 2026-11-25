"""Tests for the connection-link OAuth flow — PKCE round-trip for TikTok.

The connection-link flow is a second, public OAuth entry point (separate from
social_accounts.connect_platform). It must apply PKCE for providers that need
it (TikTok), otherwise the authorize URL lacks code_challenge and TikTok
rejects it with errCode=10007.
"""

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.onboarding.models import ConnectionLink
from apps.onboarding.views import CONNECTION_LINK_OAUTH_SESSION_KEY, _sign_connection_link_state
from providers.types import AccountProfile, OAuthTokens


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Test WS", organization=organization)


@pytest.fixture
def connection_link(db, workspace):
    return ConnectionLink.objects.create(
        workspace=workspace,
        expires_at=timezone.now() + timezone.timedelta(days=1),
    )


@pytest.mark.django_db
@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("business_login", [False, True])
def test_instagram_connection_link_uses_dm_scope_flag(client, connection_link, settings, flag, business_login):
    settings.PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED = flag
    credentials = {"app_id": "ID", "app_secret": "SECRET"}
    if business_login:
        credentials["config_id"] = "config-1"
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {"instagram": credentials}
    url = reverse("onboarding:connection_oauth_start", kwargs={"token": connection_link.token})
    response = client.post(url, {"platform": "instagram"})
    assert response.status_code == 302
    query = parse_qs(urlparse(response.url).query)
    assert query["client_id"] == ["ID"]
    assert query["response_type"] == ["code"]
    if business_login:
        assert query["config_id"] == ["config-1"]
        assert "scope" not in query
    else:
        scopes = query["scope"][0].split(",")
        assert ("instagram_manage_messages" in scopes) is flag
        assert {"pages_manage_metadata", "instagram_content_publish", "instagram_manage_comments"} <= set(scopes)


@pytest.mark.django_db
class TestConnectionLinkPkce:
    def test_oauth_start_generates_and_forwards_verifier(self, client, workspace, connection_link):
        from apps.credentials.models import PlatformCredential

        PlatformCredential.objects.create(
            organization=workspace.organization,
            platform="tiktok",
            credentials={"client_key": "k", "client_secret": "s"},
            is_configured=True,
        )

        mock_provider = MagicMock()
        mock_provider.uses_pkce = True
        mock_provider.get_auth_url.return_value = "https://www.tiktok.com/v2/auth/authorize/?ok=1"

        url = reverse("onboarding:connection_oauth_start", kwargs={"token": connection_link.token})
        with patch("apps.onboarding.views._get_provider_for_platform", return_value=mock_provider):
            response = client.post(url, {"platform": "tiktok"})

        assert response.status_code == 302
        verifier = client.session[CONNECTION_LINK_OAUTH_SESSION_KEY]["code_verifier"]
        assert verifier  # non-empty
        _, kwargs = mock_provider.get_auth_url.call_args
        assert kwargs["code_verifier"] == verifier

    def test_oauth_callback_replays_verifier(self, client, workspace, connection_link):
        nonce = "nonce-xyz"
        verifier = "stored-connection-verifier"
        state = _sign_connection_link_state(workspace.id, "tiktok", connection_link.token, nonce)
        session = client.session
        session[CONNECTION_LINK_OAUTH_SESSION_KEY] = {
            "nonce": nonce,
            "workspace_id": str(workspace.id),
            "platform": "tiktok",
            "token": connection_link.token,
            "code_verifier": verifier,
        }
        session.save()

        mock_provider = MagicMock()
        mock_provider.exchange_code.return_value = OAuthTokens(access_token="tok", refresh_token="r", expires_in=3600)
        mock_provider.get_profile.return_value = AccountProfile(platform_id="open-1", name="TT")

        url = reverse("onboarding:oauth_callback", kwargs={"platform": "social1"})
        with patch("apps.onboarding.views._get_provider_for_platform", return_value=mock_provider):
            response = client.get(url, {"code": "auth-code", "state": state})

        assert response.status_code == 302
        mock_provider.exchange_code.assert_called_once()
        _, kwargs = mock_provider.exchange_code.call_args
        assert kwargs["code_verifier"] == verifier


@pytest.mark.django_db
class TestConnectionLinkPageTokens:
    """A Page must be driven by its own token, and a Page we had to skip must
    not leave the client on a success page for an account never connected."""

    def _callback(self, client, workspace, connection_link, pages):
        nonce = "nonce-pages"
        state = _sign_connection_link_state(workspace.id, "facebook", connection_link.token, nonce)
        session = client.session
        session[CONNECTION_LINK_OAUTH_SESSION_KEY] = {
            "nonce": nonce,
            "workspace_id": str(workspace.id),
            "platform": "facebook",
            "token": connection_link.token,
        }
        session.save()

        provider = MagicMock()
        provider.exchange_code.return_value = OAuthTokens(access_token="USER-TOKEN", refresh_token="", expires_in=None)
        provider.get_profile.return_value = AccountProfile(platform_id="me-1", name="Owner")
        provider.get_user_pages.return_value = pages

        url = reverse("onboarding:oauth_callback", kwargs={"platform": "facebook"})
        with (
            patch("apps.onboarding.views._get_provider_for_platform", return_value=provider),
            patch("apps.social_accounts.views.subscribe_account_webhooks_task"),
        ):
            response = client.get(url, {"code": "auth-code", "state": state})
        return response

    def test_a_page_is_connected_with_its_own_token(self, client, workspace, connection_link):
        from apps.social_accounts.models import SocialAccount

        response = self._callback(
            client,
            workspace,
            connection_link,
            [{"id": "page-1", "name": "Page One", "access_token": "PAGE-TOKEN"}],
        )

        assert response.status_code == 302
        account = SocialAccount.objects.get(account_platform_id="page-1")
        assert account.oauth_access_token == "PAGE-TOKEN"
        assert "connection_link_error" not in client.session

    def test_a_page_without_its_own_token_is_reported_not_silently_dropped(self, client, workspace, connection_link):
        from apps.social_accounts.models import SocialAccount

        response = self._callback(
            client,
            workspace,
            connection_link,
            [
                {"id": "page-1", "name": "Page One", "access_token": "PAGE-TOKEN"},
                {"id": "page-2", "name": "Page Two"},
            ],
        )

        assert response.status_code == 302
        assert SocialAccount.objects.filter(account_platform_id="page-1").exists()
        # Never connected with the user token as a stand-in.
        assert not SocialAccount.objects.filter(account_platform_id="page-2").exists()
        error = client.session["connection_link_error"]
        assert "Page Two" in error
        assert "Page One" not in error

    # Five channels is what makes "separate credentials per channel" worth
    # asserting, and the per-platform cap would otherwise refuse the last two —
    # turning this into a test of the cap rather than of the credentials.
    @override_settings(MAX_ACCOUNTS_PER_PLATFORM=5)
    @pytest.mark.parametrize("promoted,expiry_seconds", [(True, 5184000), (False, 3600)])
    def test_instagram_link_preserves_separate_credentials_for_all_channels(
        self, client, workspace, connection_link, promoted, expiry_seconds
    ):
        from apps.social_accounts.models import SocialAccount

        started = timezone.now()
        nonce = "nonce-instagram"
        state = _sign_connection_link_state(workspace.id, "instagram", connection_link.token, nonce)
        session = client.session
        session[CONNECTION_LINK_OAUTH_SESSION_KEY] = {
            "nonce": nonce,
            "workspace_id": str(workspace.id),
            "platform": "instagram",
            "token": connection_link.token,
        }
        session.save()
        provider = MagicMock()
        provider.get_user_pages.return_value = [
            {"id": f"ig-{i}", "name": f"Channel {i}", "access_token": f"page-{i}"} for i in range(5)
        ]
        user_tokens = OAuthTokens(access_token="user", expires_in=expiry_seconds)
        with (
            patch("apps.onboarding.views._get_provider_for_platform", return_value=provider),
            patch("apps.onboarding.views.promote_meta_user_token", return_value=(user_tokens, promoted)),
            patch("apps.social_accounts.views.subscribe_account_webhooks_task"),
        ):
            response = client.get(
                reverse("onboarding:oauth_callback", kwargs={"platform": "instagram"}),
                {
                    "code": "code",
                    "state": state,
                },
            )
        assert response.status_code == 302
        assert SocialAccount.objects.filter(workspace=workspace).count() == 5
        for i in range(5):
            account = SocialAccount.objects.get(workspace=workspace, account_platform_id=f"ig-{i}")
            assert account.oauth_access_token == f"page-{i}"
            assert account.oauth_facebook_user_access_token == "user"
            assert started + timezone.timedelta(seconds=expiry_seconds) <= account.facebook_user_token_expires_at
            assert account.facebook_user_token_expires_at <= timezone.now() + timezone.timedelta(seconds=expiry_seconds)
            assert (account.token_expires_at is None) == promoted
            assert account.connection_status == "connected"


@pytest.mark.django_db
class TestConnectionLinkGoogleBusiness:
    """Nobody is on the link page to pick a business, so it is never guessed."""

    def _callback(self, client, workspace, connection_link, locations):
        nonce = "nonce-gbp"
        state = _sign_connection_link_state(workspace.id, "google_business", connection_link.token, nonce)
        session = client.session
        session[CONNECTION_LINK_OAUTH_SESSION_KEY] = {
            "nonce": nonce,
            "workspace_id": str(workspace.id),
            "platform": "google_business",
            "token": connection_link.token,
        }
        session.save()

        provider = MagicMock()
        provider.exchange_code.return_value = OAuthTokens(access_token="tok", refresh_token="r", expires_in=3600)
        provider.get_profile.return_value = AccountProfile(platform_id="locations/11", name="First listed")
        provider.get_user_pages.return_value = locations

        url = reverse("onboarding:oauth_callback", kwargs={"platform": "google_business"})
        with patch("apps.onboarding.views._get_provider_for_platform", return_value=provider):
            return client.get(url, {"code": "auth-code", "state": state}, headers={"accept-language": "en"})

    def test_a_login_with_several_businesses_connects_none(self, client, workspace, connection_link):
        from apps.social_accounts.models import SocialAccount

        response = self._callback(
            client,
            workspace,
            connection_link,
            [{"id": "locations/11", "name": "First listed"}, {"id": "locations/22", "name": "The intended one"}],
        )

        assert response.status_code == 302
        assert not SocialAccount.objects.filter(workspace=workspace, platform="google_business").exists()
        assert "more than one business" in client.session["connection_link_error"]

    def test_a_login_with_one_business_connects_it(self, client, workspace, connection_link):
        from apps.social_accounts.models import SocialAccount

        self._callback(client, workspace, connection_link, [{"id": "locations/22", "name": "The only one"}])

        account = SocialAccount.objects.get(workspace=workspace, platform="google_business")
        assert (account.account_platform_id, account.account_name) == ("locations/22", "The only one")
        assert "connection_link_error" not in client.session


class TestConnectionLinkMastodonInstance:
    """The public Mastodon form makes the server call the host it is given."""

    @pytest.mark.parametrize("instance_url", ["http://127.0.0.1:8080", "http://169.254.169.254", "https://10.0.0.5"])
    def test_a_private_address_is_refused_before_any_request(self, client, connection_link, instance_url):
        from apps.social_accounts.models import MastodonAppRegistration

        url = reverse("onboarding:connection_mastodon", kwargs={"token": connection_link.token})
        with patch("apps.onboarding.views._get_provider_for_platform") as get_provider:
            response = client.post(url, {"instance_url": instance_url})

        assert response.status_code == 302
        assert response.url == reverse("onboarding:connection_page", kwargs={"token": connection_link.token})
        get_provider.assert_not_called()
        assert not MastodonAppRegistration.objects.exists()
        assert client.session["connection_link_error"]

    def test_a_public_instance_still_starts_the_authorization(self, client, connection_link):
        mock_provider = MagicMock()
        mock_provider.register_app.return_value = {"client_id": "cid", "client_secret": "secret"}
        mock_provider.get_auth_url.return_value = "https://mastodon.example/oauth/authorize?client_id=cid"

        url = reverse("onboarding:connection_mastodon", kwargs={"token": connection_link.token})
        with (
            patch("apps.onboarding.views.is_safe_url", return_value=True),
            patch("apps.onboarding.views._get_provider_for_platform", return_value=mock_provider),
        ):
            response = client.post(url, {"instance_url": "https://mastodon.example"})

        assert response.status_code == 302
        assert response.url == "https://mastodon.example/oauth/authorize?client_id=cid"
        mock_provider.register_app.assert_called_once()


@pytest.mark.django_db
class TestConnectionLinkDone:
    """Pressing "Done" is public: it tells the owners and managers, once, while the link is alive."""

    @pytest.fixture
    def manager(self, workspace):
        from apps.accounts.models import User
        from apps.members.models import WorkspaceMembership

        manager = User.objects.create_user(
            email="manager@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        WorkspaceMembership.objects.create(
            user=manager, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.MANAGER
        )
        return manager

    def _notifications(self, user):
        from apps.notifications.models import Notification

        return Notification.objects.filter(user=user).count()

    def _done(self, client, link):
        return client.post(reverse("onboarding:connection_done", kwargs={"token": link.token}))

    def test_a_revoked_link_notifies_nobody(self, client, connection_link, manager):
        connection_link.revoked_at = timezone.now()
        connection_link.save(update_fields=["revoked_at"])

        assert self._done(client, connection_link).status_code == 400
        assert self._notifications(manager) == 0

    def test_an_expired_link_notifies_nobody(self, client, connection_link, manager):
        connection_link.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        connection_link.save(update_fields=["expires_at"])

        assert self._done(client, connection_link).status_code == 400
        assert self._notifications(manager) == 0

    def test_an_active_link_notifies_once_however_often_it_is_posted(self, client, connection_link, manager):
        responses = [self._done(client, connection_link) for _ in range(3)]

        assert [r.status_code for r in responses] == [200, 200, 200]
        assert self._notifications(manager) == 1

    def test_a_newly_connected_account_is_announced_again(self, client, workspace, connection_link, manager):
        from apps.onboarding.models import ConnectionLinkUsage
        from apps.social_accounts.models import SocialAccount

        self._done(client, connection_link)
        account = SocialAccount.objects.create(
            workspace=workspace, platform="bluesky", account_platform_id="did:1", account_name="Client"
        )
        ConnectionLinkUsage.objects.create(connection_link=connection_link, social_account=account)
        self._done(client, connection_link)

        assert self._notifications(manager) == 2
