"""Operations on the grants users gave to OAuth (MCP) clients."""

from __future__ import annotations

from oauth2_provider.models import get_access_token_model, get_grant_model, get_refresh_token_model


def revoke_user_tokens(user) -> bool:
    """End every OAuth client's access to ``user``'s account.

    Revokes the refresh tokens (so no client can mint a new access token),
    deletes the access tokens and any authorization code not yet exchanged.
    The client registrations stay: a client can be authorized again through
    a new consent. Returns True when there was a token to revoke.
    """
    had_tokens = False
    for refresh_token in get_refresh_token_model().objects.filter(user=user, revoked__isnull=True):
        refresh_token.revoke()
        had_tokens = True
    # Access tokens that no live refresh token points at any more.
    deleted, _per_model = get_access_token_model().objects.filter(user=user).delete()
    get_grant_model().objects.filter(user=user).delete()
    return had_tokens or deleted > 0
