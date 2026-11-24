"""Team accounts: the people who administer the product and never use it.

One rule holds the administrative area apart from the product: **a team
account belongs to no organization and no workspace.** With nothing to
publish from and nothing to bill, an operator cannot act as a customer,
and the only way into a customer's data is a recorded takeover
(``apps.accounts.impersonation``).

The rule is kept at every door a membership could come through:

- the signal that provisions an organization for every new user skips
  team accounts (``apps.accounts.signals``);
- a customer is never promoted (:func:`promote`) — they get a second
  account at another address;
- an operator cannot accept an invitation
  (``apps.members.services.accept_invitation``);
- ``OperatorAreaMiddleware`` keeps an operator inside the administrative
  area (``/staff/``) whatever their memberships say.

Two levels, on the flags Django already has: ``is_staff`` is support,
``is_staff`` with ``is_superuser`` is superadmin.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership

# Where a team account lands after signing in, and where it is sent back to
# from anywhere in the product.
OPERATOR_HOME = "billing_staff:staff_console"

# Where the administrative area is mounted (config/staff_urls.py). A team
# account may open anything under it.
STAFF_AREA_PREFIX = "staff/"

# Outside the area, the URL namespaces a team account may still open: its own
# profile and session (``accounts``), its own notifications (the bell in the
# shared chrome polls them) and Django's admin. Routes with no namespace
# (sign-in, language, the home redirect, legal pages) are open too.
# Everything else is the product.
OPERATOR_NAMESPACES = frozenset({"accounts", "admin", "notifications"})

# Long enough that a generated password is not worth attacking.
GENERATED_PASSWORD_LENGTH = 24


class OperatorIsCustomerError(ValueError):
    """The account belongs to an organization or a workspace, so it cannot join the team."""


class LastSuperadminError(ValueError):
    """The change would leave the product with no active superadmin."""


def is_customer(user):
    """Whether ``user`` belongs to any organization or workspace."""
    return OrgMembership.objects.filter(user=user).exists() or WorkspaceMembership.objects.filter(user=user).exists()


def operators():
    """Every team account, superadmins first.

    ``owns_something`` marks the ones that still belong to an organization or
    a workspace, which :func:`remove_operator` treats differently.
    """
    return (
        User.objects.filter(is_staff=True)
        .annotate(
            owns_something=Exists(OrgMembership.objects.filter(user=OuterRef("pk")))
            | Exists(WorkspaceMembership.objects.filter(user=OuterRef("pk")))
        )
        .order_by("-is_superuser", "email")
    )


def create_operator(email, *, password=None, name="", superuser=False):
    """Insert a team account that owns nothing. Returns the user.

    Without ``password`` the account gets a random one nobody knows; the
    person sets their own through the password-reset e-mail.

    No organization is provisioned for it: the signal that does that for
    every new user skips ``is_staff`` accounts.

    The terms are pre-accepted because ``TosAcceptanceMiddleware`` would
    otherwise hold the account on the customer-facing terms page, and an
    operator is added by the product's own team, not by signing up. The
    address counts as verified for the same reason, so signing in does not
    ask for the code a new customer account types.
    """
    from allauth.account.models import EmailAddress

    with transaction.atomic():
        user = User.objects.create_user(
            email=email,
            password=password or get_random_string(GENERATED_PASSWORD_LENGTH),
            name=name,
            is_staff=True,
            is_superuser=superuser,
            tos_accepted_at=timezone.now(),
        )
        EmailAddress.objects.create(user=user, email=user.email.lower(), primary=True, verified=True)
    return user


def promote(user, *, superuser=False):
    """Make an existing account an operator. Returns True when something changed.

    Never lowers a superadmin, and leaves the password alone. Raises
    :class:`OperatorIsCustomerError` for an account that belongs to an
    organization or a workspace.
    """
    if is_customer(user):
        raise OperatorIsCustomerError(user.email)

    fields = []
    if not user.is_staff:
        user.is_staff = True
        fields.append("is_staff")
    if superuser and not user.is_superuser:
        user.is_superuser = True
        fields.append("is_superuser")
    if fields:
        user.save(update_fields=[*fields, "updated_at"])
    return bool(fields)


def _guard_last_superadmin(user):
    """Raise when ``user`` is the only active superadmin left."""
    if not (user.is_superuser and user.is_active):
        return
    if not operators().filter(is_superuser=True, is_active=True).exclude(pk=user.pk).exists():
        raise LastSuperadminError(user.email)


def set_superadmin(user, superadmin):
    """Move a team account between support and superadmin."""
    if user.is_superuser == superadmin:
        return
    with transaction.atomic():
        if not superadmin:
            _guard_last_superadmin(user)
        user.is_superuser = superadmin
        user.save(update_fields=["is_superuser", "updated_at"])


def remove_operator(user):
    """Take ``user`` out of the team. Returns True when the account was deleted.

    A team account owns nothing, so it is deleted for good. One that
    belongs to an organization or a workspace all the same only loses its
    team flags and stays as the customer it also is: deleting it would
    leave its organizations with no owner.
    """
    with transaction.atomic():
        _guard_last_superadmin(user)
        if is_customer(user):
            user.is_staff = False
            user.is_superuser = False
            user.save(update_fields=["is_staff", "is_superuser", "updated_at"])
            return False
        user.delete()
        return True
