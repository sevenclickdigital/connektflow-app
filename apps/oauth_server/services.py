"""Operations on the grants users gave to OAuth (MCP) clients."""

from __future__ import annotations

import uuid

from oauth2_provider.models import get_access_token_model, get_grant_model, get_refresh_token_model

from .models import OAuthWorkspaceBinding


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


def current_membership(user, workspace_id=None):
    """The membership a new grant of ``user`` is bound to: the brand they have open.

    Mirrors the dashboard's "current workspace" (``RBACMiddleware``): the
    user's ``last_workspace_id`` when they still have a non-archived
    membership there, else their earliest-joined non-archived membership.
    With ``workspace_id`` (the brand the consent screen showed), that
    membership only. None when there is no usable brand.
    """
    from apps.members.models import WorkspaceMembership

    base = WorkspaceMembership.objects.select_related("workspace", "custom_role").filter(
        user=user, workspace__is_archived=False
    )
    if workspace_id is not None:
        try:
            return base.filter(workspace_id=uuid.UUID(str(workspace_id))).first()
        except ValueError:
            return None
    if getattr(user, "last_workspace_id", None):
        current = base.filter(workspace_id=user.last_workspace_id).first()
        if current is not None:
            return current
    return base.order_by("added_at").first()


def token_membership(access_token):
    """The membership an OAuth access token acts through, or None to refuse it.

    A bound token acts only on the brand it was approved for, and only while
    its user is still a member there and the brand is not archived. A token
    with no binding was issued before grants were bound and follows the
    user's open brand (:func:`current_membership`), as it always did.
    """
    from apps.members.models import WorkspaceMembership

    binding = OAuthWorkspaceBinding.objects.filter(access_token=access_token).only("workspace_id").first()
    if binding is None:
        return current_membership(access_token.user)
    return (
        WorkspaceMembership.objects.select_related("workspace", "custom_role")
        .filter(user_id=access_token.user_id, workspace_id=binding.workspace_id, workspace__is_archived=False)
        .first()
    )


def bound_workspace_id(**credential):
    """The workspace id a credential is bound to, or None when it has none.

    Takes exactly one of ``grant=``, ``access_token=`` or ``refresh_token=``.
    """
    return OAuthWorkspaceBinding.objects.filter(**credential).values_list("workspace_id", flat=True).first()


def bind(workspace_id, *, grant=None, access_token=None, refresh_token=None):
    """Bind one grant or token to ``workspace_id``; a None workspace leaves it unbound."""
    if workspace_id is None:
        return None
    return OAuthWorkspaceBinding.objects.create(
        workspace_id=workspace_id, grant=grant, access_token=access_token, refresh_token=refresh_token
    )
