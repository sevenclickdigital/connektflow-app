import logging

from allauth.account.signals import (
    email_confirmed,
    password_changed,
    password_reset,
    password_set,
    user_signed_up,
)
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext

#: Timezone a new organization starts in: the product is sold in Brazil,
#: and most of it keeps Brasília time. The browser's own timezone replaces
#: it at sign-up when the browser reports one.
SIGNUP_TIMEZONE = "America/Sao_Paulo"

logger = logging.getLogger(__name__)


def provision_organization_and_workspace(user):
    """Create a default Organization, Workspace, and memberships for a new user.

    Skips if the user already belongs to an organization (e.g. invited users).
    Safe to call multiple times - the guard is idempotent.
    """
    from apps.members.models import OrgMembership
    from apps.organizations.models import Organization

    # Skip if user was invited to an existing org or already provisioned
    if OrgMembership.objects.filter(user=user).exists():
        return

    # Its name is stored, so the brand's is written in the language active at
    # sign-up (pt-br by default) rather than translated on display.
    create_organization_with_brand(
        user,
        organization_name=Organization.DEFAULT_NAME,
        brand_name=gettext("My brand"),
        brand_description=gettext("Your default brand. Rename it anytime."),
    )


def create_organization_with_brand(user, *, organization_name, brand_name, brand_description=""):
    """Create an organization owned by ``user`` with its first brand.

    An organization is always born with a brand: the brand is what the
    user opens, and what puts the organization in the brand switcher.
    Returns the brand (a Workspace), now the user's current one.
    """
    from apps.members.models import OrgMembership, WorkspaceMembership
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    org = Organization.objects.create(
        name=organization_name,
        default_timezone=SIGNUP_TIMEZONE,
    )

    OrgMembership.objects.create(
        user=user,
        organization=org,
        org_role=OrgMembership.OrgRole.OWNER,
    )

    workspace = Workspace.objects.create(
        organization=org,
        name=brand_name,
        description=brand_description,
    )

    WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )

    # Set as last workspace so dashboard redirects here
    user.last_workspace_id = workspace.id
    user.save(update_fields=["last_workspace_id"])
    return workspace


def _set_org_timezone_from_browser(request, user):
    """Update the user's organization timezone from the browser_timezone cookie."""
    from urllib.parse import unquote
    from zoneinfo import available_timezones

    from apps.members.models import OrgMembership

    browser_tz = request.COOKIES.get("browser_timezone", "")
    # Defensive length cap: IANA timezone names are all under 40 chars.
    # Reject anything suspiciously long before decoding/validating.
    if not browser_tz or len(browser_tz) > 64:
        return
    browser_tz = unquote(browser_tz)
    if len(browser_tz) > 64 or browser_tz not in available_timezones():
        return
    membership = OrgMembership.objects.filter(user=user).select_related("organization").first()
    if membership and membership.organization.default_timezone == SIGNUP_TIMEZONE:
        membership.organization.default_timezone = browser_tz
        membership.organization.save(update_fields=["default_timezone"])


def _join_invited_org(token, user):
    """Put a just-signed-up user into the org of the invite in their session.

    In invite mode the invite was already accepted while the account was being
    created (``apps.accounts.adapters.create_account_under_invitation``, which
    makes it single-use); otherwise it is accepted here. Either way the default
    "My Organization" that post_save provisioned is then removed. Returns False
    when the invite cannot be used, so the default org stays.
    """
    from apps.members.models import Invitation, OrgMembership
    from apps.members.services import accept_invitation
    from apps.workspaces.models import Workspace

    invitation = Invitation.objects.filter(token=token).first()
    if invitation is None:
        return False
    invited_org_id = invitation.organization_id

    if invitation.accepted_at is None:
        if invitation.is_expired:
            return False
        try:
            # Skip the email-match check: the session token is proof of
            # delivery, and social logins return a provider-controlled email
            # that often differs from the invited address.
            accept_invitation(invitation, user, require_email_match=False)
        except ValueError:
            return False  # Invite acceptance failed - keep default org
    elif not OrgMembership.objects.filter(user=user, organization_id=invited_org_id).exists():
        return False  # Accepted by someone else - keep default org

    # Clean up the default org that post_save created, if it's different from
    # the invited org.
    default_memberships = OrgMembership.objects.filter(user=user).exclude(organization_id=invited_org_id)
    for membership in default_memberships:
        org = membership.organization
        membership.delete()
        # Only delete the org if it's the auto-provisioned one and has no
        # other members.
        if org.is_unnamed and not org.memberships.exists():
            Workspace.objects.filter(organization=org).delete()
            org.delete()

    # The invitation was delivered to this address, so signing up with it
    # through the link is the proof the verification code would ask for.
    if invitation.email.lower() == user.email.lower():
        from allauth.account.models import EmailAddress

        EmailAddress.objects.filter(user=user, email__iexact=user.email).update(verified=True)
    return True


@receiver(user_signed_up)
def create_organization_on_signup(sender, request, user, **kwargs):
    """Handle allauth signup - create org + workspace.

    If the user signed up via an invitation link, accept the invitation
    instead of creating a default org. The invite token is stored in
    the session by the accept_invite view.

    By this point, post_save has already fired and provisioned a default
    "My Organization". If invite acceptance succeeds, we clean up that
    default org so the user only belongs to the invited org.
    """
    # The e-mail sign-up form states the terms under its button, so filling it
    # in is the acceptance, for someone arriving by an invitation as well.
    # A social sign-up never saw that form and is sent to the terms page.
    if not kwargs.get("sociallogin"):
        user.tos_accepted_at = timezone.now()
        user.save(update_fields=["tos_accepted_at"])

    pending_token = request.session.pop("pending_invite_token", None)
    if pending_token and _join_invited_org(pending_token, user):
        return  # Done - user is now in the invited org only

    # No invite or invite failed - ensure default provisioning happened.
    # post_save already handled this, so this is a no-op (idempotent guard).
    provision_organization_and_workspace(user)

    # The account came by an affiliate's link: it is that affiliate's referral.
    # A failure here must not undo the sign-up.
    from apps.affiliates import services as affiliate_services

    try:
        affiliate_services.attribute_signup(request, user)
    except Exception:
        logger.exception("Could not attribute the sign-up of user %s to an affiliate", user.pk)

    # Try to set the organization timezone from the browser cookie set during signup.
    _set_org_timezone_from_browser(request, user)


def _send_welcome_email(request, user):
    """Queue the welcome email for after the commit.

    Sent through allauth's adapter so it gets the same layout, brand context and
    mail class as the rest of the account mail (``account/email/welcome_*``).
    After the commit, so a request that rolls back does not greet anyone, and a
    failed send is logged without touching the account.
    """
    from allauth.account.adapter import get_adapter

    if not user.email:
        return

    def _send():
        try:
            get_adapter(request).send_mail("account/email/welcome", user.email, {"user": user})
        except Exception:
            logger.exception("Could not send the welcome email to user %s", user.pk)

    transaction.on_commit(_send)


@receiver(user_signed_up)
def welcome_an_account_that_arrives_verified(sender, request, user, **kwargs):
    """Welcome at sign-up whoever has nothing left to prove: a Google sign-up, or an invitee on the invited address.

    Everyone else is welcomed when they type the verification code
    (``welcome_once_the_address_is_confirmed``), so the welcome never reaches
    an address nobody confirmed. Registered after
    ``create_organization_on_signup``, which is what marks an invitee's address.
    """
    from allauth.account.models import EmailAddress

    if EmailAddress.objects.filter(user=user, verified=True).exists():
        _send_welcome_email(request, user)


@receiver(email_confirmed)
def welcome_once_the_address_is_confirmed(sender, request, email_address, **kwargs):
    """Welcome a new account when it confirms its address, which is its first verified one."""
    from allauth.account.models import EmailAddress

    user = email_address.user
    if EmailAddress.objects.filter(user=user, verified=True).exclude(pk=email_address.pk).exists():
        return
    _send_welcome_email(request, user)


@receiver(password_changed)
@receiver(password_reset)
@receiver(password_set)
def revoke_oauth_tokens_on_new_password(sender, request, user, **kwargs):
    """A new password ends the access every OAuth (MCP) client had.

    Changing or resetting the password is how an owner takes the account
    back, and the grants were made from sessions that the new password
    ends too. API keys are not touched: they are listed and revoked on
    the organization's API keys page.
    """
    from apps.oauth_server.services import revoke_user_tokens

    revoke_user_tokens(user)


def _request_ip(request):
    from apps.accounts.impersonation import client_ip

    return (client_ip(request) if request is not None else None) or "-"


def _masked_login(value):
    """``ana.souza@example.com`` -> ``an***@example.com``: enough to spot one
    address being hammered without writing the address into the logs."""
    local, _, domain = str(value or "").partition("@")
    return f"{local[:2]}***@{domain}" if domain else f"{local[:2]}***"


@receiver(user_login_failed)
def log_failed_login(sender, credentials, request=None, **kwargs):
    """Leave a trace of every wrong password, so a guessing run shows up in the logs."""
    login = credentials.get("email") or credentials.get("username") or credentials.get("login")
    logger.warning(
        "Failed login for %s from %s on %s",
        _masked_login(login),
        _request_ip(request),
        getattr(request, "path", "-"),
    )


@receiver(user_logged_in)
def log_team_login(sender, request, user, **kwargs):
    """Team accounts reach every customer, so each of their sign-ins is recorded."""
    if user.is_staff:
        logger.info("Team account %s signed in from %s", user.pk, _request_ip(request))


@receiver(post_save, sender="accounts.User")
def create_organization_on_user_create(sender, instance, created, **kwargs):
    """Handle any user creation path (admin, shell).

    The allauth signal fires *after* post_save, so for normal signups
    post_save runs first and the allauth handler is a no-op (idempotent guard).

    A team account (``is_staff``, which ``createsuperuser`` also sets) gets
    nothing: it never belongs to an organization or a workspace, see
    ``apps/accounts/operators.py``.
    """
    if created and not instance.is_staff:
        provision_organization_and_workspace(instance)
