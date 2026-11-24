"""OAuth Authorization Server endpoints for the MCP connector flow.

django-oauth-toolkit supplies /oauth/authorize/ and /oauth/token/. This module
adds the two pieces it does not ship:
  - RFC 7591 Dynamic Client Registration (POST /oauth/register)
  - RFC 8414 / RFC 9728 discovery metadata documents

DCR is intentionally open — any MCP client may register. A registered client
still cannot obtain a token without an Connekt Flow user completing login
and consent, so the exposure is bounded; registration is rate-limited only to
blunt row-spam abuse.
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from oauth2_provider import views as oauth2_views
from oauth2_provider.models import get_application_model

from apps.accounts import impersonation

from . import services
from .metadata import authorization_server_metadata, protected_resource_metadata
from .validator import CONSENT_WORKSPACE_ATTR

LOG = logging.getLogger(__name__)

_MAX_REDIRECT_URIS = 5
_SUPPORTED_GRANTS = {"authorization_code", "refresh_token"}
# Per-IP cap is the primary defense — one abuser shouldn't be able to block
# legitimate MCP client onboarding. The much-higher server-wide cap stays as
# a backstop against distributed spray that the per-IP limit alone wouldn't
# catch.
_DCR_PER_IP_LIMIT = 10
_DCR_GLOBAL_LIMIT = 1000
_DCR_RATE_WINDOW = 60 * 60  # seconds
_DCR_PER_IP_KEY_PREFIX = "oauth_dcr_ip:"
_DCR_GLOBAL_KEY = "oauth_dcr_registrations_global"


class AuthorizationView(oauth2_views.AuthorizationView):
    """The consent screen, closed to impersonation sessions.

    A grant outlives the browser session that approved it, so one approved
    by an operator inside a customer's account would keep acting as the
    customer after the takeover ended.
    """

    def dispatch(self, request, *args, **kwargs):
        if impersonation.is_impersonating(request):
            raise PermissionDenied("Apps cannot be connected during an impersonation session.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        """Add ``redirect_host``: where the access will actually go.

        Registration is open and ``client_name`` is whatever the registrant
        typed, so "Claude Desktop" on the screen proves nothing. The host the
        code is sent to is the one fact the person can check.

        Also ``consent_workspace``: the brand the grant will be bound to.
        """
        context = super().get_context_data(**kwargs)
        redirect_uri = context.get("redirect_uri")
        form = context.get("form")
        if not redirect_uri and form is not None and "redirect_uri" in form.fields:
            redirect_uri = form["redirect_uri"].value()
        context["redirect_host"] = urlparse(redirect_uri or "").hostname or ""
        membership = services.current_membership(self.request.user)
        context["consent_workspace"] = membership.workspace if membership is not None else None
        return context

    def create_authorization_response(self, request, scopes, credentials, allow):
        """Bind the grant to the brand being approved (``apps.oauth_server.models``).

        The consent form posts the brand it showed, so switching brands in
        another tab between opening the screen and approving cannot change
        what was approved. The automatic re-approval of a client that already
        holds a valid token has no form and binds the brand open now.
        """
        if allow:
            posted = request.POST.get("workspace") if request.method == "POST" else None
            membership = services.current_membership(request.user, workspace_id=posted or None)
            if posted and membership is None:
                raise PermissionDenied(
                    gettext("That brand is no longer available to you. Start the connection again from the app.")
                )
            if membership is not None:
                credentials[CONSENT_WORKSPACE_ATTR] = membership.workspace_id
        return super().create_authorization_response(request, scopes, credentials, allow)


def _dcr_error(error: str, description: str, status: int = 400) -> JsonResponse:
    """RFC 7591 section 3.2.2 error response."""
    return JsonResponse(
        {"error": error, "error_description": description},
        status=status,
    )


def _client_ip(request) -> str:
    """Return the client IP used for rate-limiting.

    Uses ``REMOTE_ADDR`` directly. Behind a proxy this is the proxy address —
    accurate per-client attribution would require a vetted ``X-Forwarded-For``
    parsing chain, and we deliberately don't trust an arbitrary forwarded
    header here. The global backstop catches mass spray.
    """
    return request.META.get("REMOTE_ADDR", "unknown") or "unknown"


def _incr_counter(key: str) -> int:
    """Atomic increment with TTL bootstrap."""
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, _DCR_RATE_WINDOW)
        return 1


def _dcr_rate_limited(request) -> bool:
    """Best-effort per-IP cap with a server-wide backstop to blunt row-spam.

    Once an IP is over its per-IP cap we short-circuit *before* touching the
    global counter. Otherwise a single abuser would still burn through the
    global budget on every blocked request and would eventually 429 every
    legitimate client — the exact scenario the per-IP design exists to
    prevent.
    """
    per_ip_count = _incr_counter(f"{_DCR_PER_IP_KEY_PREFIX}{_client_ip(request)}")
    if per_ip_count > _DCR_PER_IP_LIMIT:
        return True
    global_count = _incr_counter(_DCR_GLOBAL_KEY)
    return global_count > _DCR_GLOBAL_LIMIT


def _is_https_uri(value) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


@method_decorator(csrf_exempt, name="dispatch")
class RegisterView(View):
    """RFC 7591 Dynamic Client Registration.

    Registers a public, authorization-code OAuth client (PKCE — no secret).
    """

    def post(self, request, *args, **kwargs):
        if _dcr_rate_limited(request):
            return _dcr_error(
                "temporarily_unavailable",
                "Registration rate limit reached. Retry later.",
                status=429,
            )

        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return _dcr_error("invalid_client_metadata", "Request body must be JSON.")
        if not isinstance(body, dict):
            return _dcr_error(
                "invalid_client_metadata",
                "Request body must be a JSON object.",
            )

        redirect_uris = body.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _dcr_error("invalid_redirect_uri", "redirect_uris is required.")
        if len(redirect_uris) > _MAX_REDIRECT_URIS:
            return _dcr_error("invalid_redirect_uri", "Too many redirect_uris.")
        if not all(_is_https_uri(u) for u in redirect_uris):
            return _dcr_error(
                "invalid_redirect_uri",
                "Each redirect_uri must be an absolute https URL.",
            )

        grant_types = body.get("grant_types") or ["authorization_code"]
        if not isinstance(grant_types, list) or set(grant_types) - _SUPPORTED_GRANTS:
            return _dcr_error(
                "invalid_client_metadata",
                "Only authorization_code and refresh_token grants are supported.",
            )

        auth_method = body.get("token_endpoint_auth_method", "none")
        if auth_method != "none":
            return _dcr_error(
                "invalid_client_metadata",
                "Only public clients (token_endpoint_auth_method 'none') are supported.",
            )

        client_name = body.get("client_name") or "MCP client"
        if not isinstance(client_name, str):
            return _dcr_error("invalid_client_metadata", "client_name must be a string.")
        client_name = client_name[:255]

        application_model = get_application_model()
        app = application_model.objects.create(
            name=client_name,
            client_type=application_model.CLIENT_PUBLIC,
            authorization_grant_type=application_model.GRANT_AUTHORIZATION_CODE,
            redirect_uris=" ".join(redirect_uris),
            skip_authorization=False,
        )
        LOG.info(
            "OAuth DCR: registered client_id=%s name=%r",
            app.client_id,
            client_name,
        )

        return JsonResponse(
            {
                "client_id": app.client_id,
                "client_id_issued_at": int(time.time()),
                "redirect_uris": redirect_uris,
                "grant_types": sorted(set(grant_types) | {"authorization_code"}),
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "client_name": client_name,
                "scope": "mcp",
            },
            status=201,
        )


@require_GET
def authorization_server_metadata_view(request):
    """RFC 8414 — served at /.well-known/oauth-authorization-server."""
    return JsonResponse(authorization_server_metadata())


@require_GET
def protected_resource_metadata_view(request):
    """RFC 9728 — served at /.well-known/oauth-protected-resource[/api/v1/mcp]."""
    return JsonResponse(protected_resource_metadata())
