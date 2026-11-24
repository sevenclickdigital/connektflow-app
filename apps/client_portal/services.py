"""Client Portal business logic.

Handles magic link generation, verification, and portal session creation.
"""

import logging

from django.conf import settings
from django.contrib.auth import login
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.common.emails import build_email, in_language_of
from apps.common.mail import send_or_raise, transactional
from apps.members.models import WorkspaceMembership

from .middleware import PORTAL_SESSION_KEY, PORTAL_WORKSPACE_KEY
from .models import MagicLinkToken, _generate_magic_token, hash_magic_token

logger = logging.getLogger(__name__)

# How long a portal session lasts after a magic link is redeemed. The session
# is confined to /portal/ (``middleware.PortalSessionConfinementMiddleware``),
# so its length only buys the client fewer links to ask for.
PORTAL_SESSION_AGE = 30 * 24 * 60 * 60


def issue_magic_link_token(user, workspace):
    """Create a MagicLinkToken and return ``(token, raw_token)``.

    Only the hash is stored. The raw token is returned once, for the e-mailed
    URL, and cannot be recovered from the database afterwards.
    """
    raw_token = _generate_magic_token()
    token = MagicLinkToken.objects.create(user=user, workspace=workspace, token_hash=hash_magic_token(raw_token))
    return token, raw_token


def _find_token(raw_token):
    if not raw_token:
        return None
    return (
        MagicLinkToken.objects.select_related("user", "workspace")
        .filter(token_hash=hash_magic_token(raw_token))
        .first()
    )


def generate_magic_link(workspace, client_user, created_by):
    """Generate a magic link for a client user and send it via email.

    Args:
        workspace: The Workspace the client belongs to.
        client_user: The User with client role.
        created_by: The User (manager) who initiated the generation.

    Returns:
        The created MagicLinkToken.

    Raises:
        ValueError: If the user is not a client of this workspace, or the email
            could not be sent — in which case no new token is left behind and
            any existing link keeps working.
    """
    # Validate client has client role in workspace
    membership = WorkspaceMembership.objects.filter(
        user=client_user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.CLIENT,
    ).first()

    if not membership:
        raise ValueError("User does not have client role in this workspace.")

    # The previously issued tokens are NOT invalidated yet. They used to be
    # revoked here, before the new link was sent, which meant a send that did
    # not happen — a refused address, the outbound budget, a broken mail
    # server — left the client with no way in at all while the manager was told
    # the link had gone out. The old links are revoked further down, once the
    # replacement is known to have left.
    superseded = list(
        MagicLinkToken.objects.filter(
            user=client_user,
            workspace=workspace,
            is_consumed=False,
            expires_at__gt=timezone.now(),
        ).values_list("pk", flat=True)
    )

    token, raw_token = issue_magic_link_token(client_user, workspace)

    app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
    magic_url = f"{app_url}/portal/{raw_token}/"

    # Send email
    context = {
        "client_user": client_user,
        "workspace": workspace,
        "magic_url": magic_url,
        "created_by": created_by,
        "app_url": app_url,
    }

    with in_language_of(client_user.email):
        msg = build_email(
            subject=_("%(workspace)s - Posts ready for your review") % {"workspace": workspace.name},
            template_prefix="client_portal/email/magic_link",
            context=context,
            to=[client_user.email],
            # A login link a manager just asked us to send: the recipient is waiting
            # for it, so the per-recipient notification cap must not eat it.
            headers=transactional(),
        )

    try:
        send_or_raise(msg)
    except Exception as exc:
        logger.exception("Failed to send magic link email to %s", client_user.email)
        # Roll the new token back so we do not leave a link nobody was told
        # about, and leave the client's existing one working.
        MagicLinkToken.objects.filter(pk=token.pk).delete()
        raise ValueError("We could not send that link right now. Please try again shortly.") from exc

    # The replacement is on its way, so the old links can go.
    if superseded:
        MagicLinkToken.objects.filter(pk__in=superseded).update(expires_at=timezone.now())

    return token


def peek_magic_link(token_string):
    """Validate a magic link token *without* consuming it.

    Used to render the confirmation page on GET so that email link scanners
    that prefetch the URL cannot burn the one-time token. Returns the
    MagicLinkToken when it is valid and unused, otherwise None.
    """
    token = _find_token(token_string)
    if token is None:
        return None

    if token.is_expired or token.is_consumed:
        return None

    return token


def consume_magic_link(token_string):
    """Validate and atomically consume a magic link token (single-use).

    Returns:
        (user, workspace, is_valid) tuple.
        If missing/expired/already consumed: (None, None, False).
    """
    token = _find_token(token_string)
    if token is None:
        return None, None, False

    if token.is_expired:
        return None, None, False

    # Atomic single-use: only the request that flips is_consumed False->True wins.
    consumed = MagicLinkToken.objects.filter(pk=token.pk, is_consumed=False).update(
        is_consumed=True,
        last_used_at=timezone.now(),
    )
    if not consumed:
        return None, None, False

    return token.user, token.workspace, True


def revoke_magic_link(token_id, workspace):
    """Revoke an active magic link by expiring it immediately."""
    MagicLinkToken.objects.filter(
        id=token_id,
        workspace=workspace,
        expires_at__gt=timezone.now(),
    ).update(expires_at=timezone.now())


def create_portal_session(request, user, workspace):
    """Log the client in with a session confined to the portal of ``workspace``.

    ``login`` clears any earlier portal flags (``middleware.clear_portal_flags``)
    before they are set again here.
    """
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session[PORTAL_SESSION_KEY] = True
    request.session[PORTAL_WORKSPACE_KEY] = str(workspace.id)
    request.session.set_expiry(PORTAL_SESSION_AGE)
