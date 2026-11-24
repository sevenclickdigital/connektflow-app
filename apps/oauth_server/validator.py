"""Custom OAuth2 validator: S256-only PKCE, and grants bound to a brand.

The binding half is described in ``apps.oauth_server.models``.

django-oauth-toolkit's default validator (and oauthlib under it) accepts both
``S256`` and ``plain`` for ``code_challenge_method``. RFC 7636 §4.2 marks
``plain`` as insecure unless the channel is fully secure end-to-end — which
defeats the entire point of PKCE for the kind of MCP-client scenarios we
support. The protected-resource metadata document advertises ``S256`` as the
only supported method (see ``apps.oauth_server.metadata``); this validator
enforces that contract at the authorize endpoint, blocking authorization
requests with a missing or ``plain`` method early — before any Grant row is
written.
"""

from __future__ import annotations

from oauth2_provider.models import get_grant_model
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.oauth2.rfc6749 import errors as oauthlib_errors

from . import services

#: Attribute the consent view sets on the oauthlib request (through the
#: authorization ``credentials``) with the id of the brand being approved.
CONSENT_WORKSPACE_ATTR = "connekt_workspace_id"


class S256OnlyOAuth2Validator(OAuth2Validator):
    """OAuth2Validator subclass that requires ``code_challenge_method=S256``
    and binds every grant and token to the brand it was approved for.

    Hooks ``is_pkce_required`` because it is the earliest validator method
    oauthlib invokes while ``request.code_challenge_method`` is still the
    raw client-supplied value (oauthlib later defaults a missing method to
    ``"plain"`` at ``authorization_code.py``; we reject before that lands).
    """

    def is_pkce_required(self, client_id, request):
        code_challenge = getattr(request, "code_challenge", None)
        method = getattr(request, "code_challenge_method", None)
        if code_challenge and method != "S256":
            raise oauthlib_errors.InvalidRequestError(
                description="code_challenge_method must be S256",
                request=request,
            )
        return super().is_pkce_required(client_id, request)

    # -- workspace binding (apps.oauth_server.models) -----------------------
    #
    # The consent binds the authorization code; exchanging the code binds the
    # access and refresh tokens it yields; a refresh carries the binding of the
    # refresh token it consumes into the new pair. A chain that started before
    # bindings existed stays unbound, and keeps the fallback.

    def _create_authorization_code(self, request, code, expires=None):
        grant = super()._create_authorization_code(request, code, expires=expires)
        services.bind(getattr(request, CONSENT_WORKSPACE_ATTR, None), grant=grant)
        return grant

    def _create_access_token(self, expires, request, token, source_refresh_token=None):
        access_token = super()._create_access_token(expires, request, token, source_refresh_token=source_refresh_token)
        if source_refresh_token is not None:
            workspace_id = services.bound_workspace_id(refresh_token=source_refresh_token)
        elif request.grant_type == "authorization_code":
            # The grant is deleted only after the tokens are saved.
            grant = get_grant_model().objects.filter(code=request.code, application=request.client).first()
            workspace_id = services.bound_workspace_id(grant=grant) if grant is not None else None
        else:
            workspace_id = None
        services.bind(workspace_id, access_token=access_token)
        return access_token

    def _create_refresh_token(self, request, refresh_token_code, access_token, previous_refresh_token):
        refresh_token = super()._create_refresh_token(request, refresh_token_code, access_token, previous_refresh_token)
        services.bind(services.bound_workspace_id(access_token=access_token), refresh_token=refresh_token)
        return refresh_token
