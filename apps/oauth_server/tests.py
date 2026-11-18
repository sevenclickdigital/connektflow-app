"""Tests for the OAuth Authorization Server backing the MCP connector flow.

Covers Dynamic Client Registration (RFC 7591), the RFC 8414 / RFC 9728
discovery documents, and the S256-only PKCE enforcement.
"""

from __future__ import annotations

import json

import pytest
from django.test import Client

REGISTER_URL = "/oauth/register"
AS_META_URL = "/.well-known/oauth-authorization-server"
PR_META_URL = "/.well-known/oauth-protected-resource"
PR_META_MCP_URL = "/.well-known/oauth-protected-resource/api/v1/mcp"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture(autouse=True)
def _clear_dcr_rate_limit():
    """DCR counters live in the shared cache keyed on 127.0.0.1 — reset them."""
    from django.core.cache import cache

    keys = ("oauth_dcr_ip:127.0.0.1", "oauth_dcr_registrations_global")
    for k in keys:
        cache.delete(k)
    yield
    for k in keys:
        cache.delete(k)


def _register(client: Client, body: dict):
    return client.post(REGISTER_URL, data=json.dumps(body), content_type="application/json")


@pytest.mark.django_db
class TestDynamicClientRegistration:
    def test_register_public_client(self):
        r = _register(
            Client(),
            {"client_name": "Claude", "redirect_uris": [CLAUDE_REDIRECT], "token_endpoint_auth_method": "none"},
        )
        assert r.status_code == 201
        data = r.json()
        assert data["client_id"]
        assert data["token_endpoint_auth_method"] == "none"
        assert data["redirect_uris"] == [CLAUDE_REDIRECT]
        assert data["scope"] == "mcp"

        from oauth2_provider.models import get_application_model

        app = get_application_model().objects.get(client_id=data["client_id"])
        assert app.client_type == app.CLIENT_PUBLIC
        assert app.authorization_grant_type == app.GRANT_AUTHORIZATION_CODE

    def test_rejects_http_redirect(self):
        r = _register(Client(), {"redirect_uris": ["http://evil.example.com/cb"]})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_redirect_uri"

    def test_rejects_missing_redirect(self):
        r = _register(Client(), {"client_name": "x"})
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_redirect_uri"

    def test_rejects_confidential_client(self):
        r = _register(
            Client(),
            {"redirect_uris": [CLAUDE_REDIRECT], "token_endpoint_auth_method": "client_secret_basic"},
        )
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client_metadata"

    def test_rejects_non_json_body(self):
        r = Client().post(REGISTER_URL, data="not json", content_type="application/json")
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client_metadata"

    def test_rate_limited_after_per_ip_cap(self):
        c = Client()
        for _ in range(10):  # per-IP cap is 10 → first 10 succeed
            assert _register(c, {"redirect_uris": [CLAUDE_REDIRECT]}).status_code == 201
        r = _register(c, {"redirect_uris": [CLAUDE_REDIRECT]})  # 11th is blocked
        assert r.status_code == 429
        assert r.json()["error"] == "temporarily_unavailable"


@pytest.mark.django_db
class TestDiscoveryMetadata:
    def test_authorization_server_metadata(self):
        data = Client().get(AS_META_URL).json()
        assert data["issuer"]
        assert data["authorization_endpoint"].endswith("/oauth/authorize/")
        assert data["token_endpoint"].endswith("/oauth/token/")
        assert data["registration_endpoint"].endswith("/oauth/register")
        assert data["revocation_endpoint"].endswith("/oauth/revoke_token/")
        assert data["code_challenge_methods_supported"] == ["S256"]
        assert data["token_endpoint_auth_methods_supported"] == ["none"]
        assert data["scopes_supported"] == ["mcp"]

    def test_protected_resource_metadata(self):
        data = Client().get(PR_META_URL).json()
        assert data["resource"].endswith("/api/v1/mcp")
        assert data["scopes_supported"] == ["mcp"]
        assert data["authorization_servers"]

    def test_protected_resource_metadata_path_scoped(self):
        # RFC 9728 path-scoped variant — what the WWW-Authenticate header points at.
        data = Client().get(PR_META_MCP_URL).json()
        assert data["resource"].endswith("/api/v1/mcp")


class _FakeOAuthRequest:
    """Minimal oauthlib-style request for the validator unit tests.

    Unset attributes read as ``None`` so oauthlib's error constructor — which
    copies ``redirect_uri`` / ``state`` / etc. off the request — doesn't trip.
    """

    def __init__(self, *, code_challenge_method):
        self.code_challenge = "abc123"
        self.code_challenge_method = code_challenge_method

    def __getattr__(self, name):
        return None


class TestS256PkceEnforcement:
    def test_plain_method_rejected(self):
        from oauthlib.oauth2.rfc6749 import errors as oauthlib_errors

        from apps.oauth_server.validator import S256OnlyOAuth2Validator

        with pytest.raises(oauthlib_errors.InvalidRequestError):
            S256OnlyOAuth2Validator().is_pkce_required(
                "client-id",
                _FakeOAuthRequest(code_challenge_method="plain"),
            )

    def test_s256_method_allowed(self):
        from apps.oauth_server.validator import S256OnlyOAuth2Validator

        # Does not raise; defers to the parent's PKCE_REQUIRED decision.
        assert S256OnlyOAuth2Validator().is_pkce_required(
            "client-id",
            _FakeOAuthRequest(code_challenge_method="S256"),
        ) in (True, False)


# ---------------------------------------------------------------------------
# Grant lifetime: a grant ends with the password it was made under, and none
# is made from an impersonation session.
# ---------------------------------------------------------------------------

MCP_URL = "/api/v1/mcp/"
OLD_PASSWORD = "old-password-123"
NEW_PASSWORD = "new-password-456"


class _SecureClient(Client):
    """The OAuth endpoints and the MCP transport are HTTPS-only."""

    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


def _account(email):
    """A user who owns a workspace, which the MCP bearer check requires."""
    from django.utils import timezone

    from apps.accounts.models import User
    from apps.members.models import WorkspaceMembership
    from apps.workspaces.models import Workspace

    user = User.objects.create_user(email=email, password=OLD_PASSWORD, tos_accepted_at=timezone.now())
    workspace = Workspace.objects.create(name="Grant WS", organization=user.org_memberships.first().organization)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    user.last_workspace_id = workspace.id
    user.save(update_fields=["last_workspace_id"])
    return user


def _authorize_params(browser):
    """Register a client and build its authorize request. Returns (params, verifier)."""
    import base64
    import hashlib
    import secrets

    registered = _register(browser, {"client_name": "Helper", "redirect_uris": [CLAUDE_REDIRECT]})
    assert registered.status_code == 201
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "response_type": "code",
        "client_id": registered.json()["client_id"],
        "redirect_uri": CLAUDE_REDIRECT,
        "scope": "mcp",
        "state": "s1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return params, verifier


def _mint_grant(browser):
    """Consent and code exchange from a logged-in browser. Returns (client_id, access, refresh)."""
    from urllib.parse import parse_qs, urlparse

    params, verifier = _authorize_params(browser)
    consent = browser.post("/oauth/authorize/", {**params, "allow": "Authorize"})
    assert consent.status_code == 302
    code = parse_qs(urlparse(consent["Location"]).query)["code"][0]
    token = _SecureClient().post(
        "/oauth/token/",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_REDIRECT,
            "client_id": params["client_id"],
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200
    return params["client_id"], token.json()["access_token"], token.json()["refresh_token"]


def _bearer_status(access_token):
    return (
        _SecureClient(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        .post(MCP_URL, data=json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1}), content_type="application/json")
        .status_code
    )


def _refresh_status(client_id, refresh_token):
    return (
        _SecureClient()
        .post("/oauth/token/", {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id})
        .status_code
    )


def _logged_in(user):
    browser = _SecureClient()
    browser.force_login(user)
    return browser


@pytest.fixture
def _clear_auth_rate_limit():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_auth_rate_limit")
class TestGrantEndsWithThePassword:
    def test_password_change_revokes_the_grant(self):
        owner = _account("grant-owner@example.com")
        bystander = _account("grant-bystander@example.com")
        client_id, access, refresh = _mint_grant(_logged_in(owner))
        other_client_id, other_access, other_refresh = _mint_grant(_logged_in(bystander))
        assert _bearer_status(access) == 200

        response = _logged_in(owner).post(
            "/accounts/settings/",
            {
                "action": "update_password",
                "current_password": OLD_PASSWORD,
                "password": NEW_PASSWORD,
                "password_confirm": NEW_PASSWORD,
            },
        )

        assert response.status_code == 302
        owner.refresh_from_db()
        assert owner.check_password(NEW_PASSWORD)
        assert _bearer_status(access) == 401
        assert _refresh_status(client_id, refresh) == 400
        # Another account's grant is not touched.
        assert _bearer_status(other_access) == 200
        assert _refresh_status(other_client_id, other_refresh) == 200

    def test_rejected_password_change_keeps_the_grant(self):
        owner = _account("grant-owner@example.com")
        client_id, access, refresh = _mint_grant(_logged_in(owner))

        _logged_in(owner).post(
            "/accounts/settings/",
            {
                "action": "update_password",
                "current_password": "not-the-password",
                "password": NEW_PASSWORD,
                "password_confirm": NEW_PASSWORD,
            },
        )

        owner.refresh_from_db()
        assert owner.check_password(OLD_PASSWORD)
        assert _bearer_status(access) == 200
        assert _refresh_status(client_id, refresh) == 200

    def test_password_reset_by_email_revokes_the_grant(self):
        import re
        from urllib.parse import urlparse

        from django.core import mail

        owner = _account("grant-owner@example.com")
        client_id, access, refresh = _mint_grant(_logged_in(owner))

        anonymous = _SecureClient()
        anonymous.post("/accounts/password/reset/", {"email": owner.email})
        link = re.search(r"https?://[^\s]+/accounts/password/reset/key/[^\s/]+/", mail.outbox[-1].body).group(0)
        page = anonymous.get(urlparse(link).path)
        form_path = page["Location"] if page.status_code == 302 else urlparse(link).path
        anonymous.post(form_path, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD})

        owner.refresh_from_db()
        assert owner.check_password(NEW_PASSWORD)
        assert _bearer_status(access) == 401
        assert _refresh_status(client_id, refresh) == 400


@pytest.mark.django_db
@pytest.mark.usefixtures("_clear_auth_rate_limit")
class TestNoGrantFromAnImpersonationSession:
    def test_consent_is_refused_while_impersonating(self):
        from django.utils import timezone
        from oauth2_provider.models import get_access_token_model, get_grant_model

        from apps.accounts.models import User

        customer = _account("grant-customer@example.com")
        operator = User.objects.create_user(
            email="grant-operator@example.com", password=OLD_PASSWORD, is_staff=True, tos_accepted_at=timezone.now()
        )
        browser = _logged_in(operator)
        assert browser.post(f"/accounts/impersonate/{customer.pk}/").status_code == 302
        params, _verifier = _authorize_params(browser)

        assert browser.get("/oauth/authorize/", params).status_code == 403
        assert browser.post("/oauth/authorize/", {**params, "allow": "Authorize"}).status_code == 403
        assert not get_grant_model().objects.filter(user=customer).exists()
        assert not get_access_token_model().objects.filter(user=customer).exists()

    def test_the_account_owner_still_reaches_the_consent_screen(self):
        browser = _logged_in(_account("grant-owner@example.com"))
        params, _verifier = _authorize_params(browser)

        assert browser.get("/oauth/authorize/", params).status_code == 200
