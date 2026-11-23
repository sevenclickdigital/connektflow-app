"""Team: the accounts that administer the product, and their level.

Superadmin only. Every rule about what a team account is lives in
``apps.accounts.operators``; these views read the form, call it and say
what happened.

Nobody changes or removes their own account here: it would be the
quickest way to lock the last person out of this very screen.
"""

from __future__ import annotations

import logging

from allauth.account.forms import ResetPasswordForm
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts import operators
from apps.accounts.decorators import superadmin_required
from apps.accounts.models import User
from apps.common.validators import clean_display_name

logger = logging.getLogger(__name__)

ROLE_SUPERADMIN = "superadmin"
ROLE_SUPPORT = "support"


def _back():
    return redirect("accounts_staff:team")


def _other_operator(request, user_id):
    """The team account in the URL, refusing the requester's own."""
    target = get_object_or_404(operators.operators(), pk=user_id)
    if target.pk == request.user.pk:
        raise PermissionDenied("You cannot change your own team account.")
    return target


@superadmin_required
@require_GET
def team(request):
    return render(
        request,
        "accounts/staff_team.html",
        {"admin_active": "team", "members": operators.operators()},
    )


@superadmin_required
@require_POST
def team_add(request):
    """Add a person to the team, by creating their account or promoting a spare one.

    A new account gets a password nobody knows and the same reset e-mail
    "Forgot your password?" sends, so the person sets their own.
    """
    email = User.objects.normalize_email(request.POST.get("email", "").strip())
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, _("Enter a valid e-mail address."))
        return _back()
    superuser = request.POST.get("role") == ROLE_SUPERADMIN

    existing = User.objects.filter(email__iexact=email).first()
    if existing is not None:
        try:
            operators.promote(existing, superuser=superuser)
        except operators.OperatorIsCustomerError:
            messages.error(
                request,
                _(
                    "%(email)s is a customer account. A team account cannot belong to an "
                    "organization or a brand: use another e-mail address."
                )
                % {"email": email},
            )
            return _back()
        logger.warning("Operator %s added %s to the team", request.user.email, existing.email)
        messages.success(request, _("%(email)s is now part of the team.") % {"email": existing.email})
        return _back()

    user = operators.create_operator(email, name=clean_display_name(request.POST.get("name", "")), superuser=superuser)
    logger.warning("Operator %s created the team account %s", request.user.email, user.email)

    form = ResetPasswordForm(data={"email": user.email})
    if form.is_valid():
        form.save(request)
        messages.success(
            request,
            _("%(email)s was added. An e-mail to set the password was sent to them.") % {"email": user.email},
        )
    else:
        logger.warning("Password e-mail for %s not sent: %s", user.email, form.errors.as_json())
        messages.warning(
            request,
            _(
                "%(email)s was added, but the e-mail to set the password was not sent. Ask them to use “Forgot your password?”."
            )
            % {"email": user.email},
        )
    return _back()


@superadmin_required
@require_POST
def team_set_role(request, user_id):
    target = _other_operator(request, user_id)
    try:
        operators.set_superadmin(target, request.POST.get("role") == ROLE_SUPERADMIN)
    except operators.LastSuperadminError:
        messages.error(request, _("The team needs at least one active superadmin."))
        return _back()
    logger.warning(
        "Operator %s set %s to %s",
        request.user.email,
        target.email,
        ROLE_SUPERADMIN if target.is_superuser else ROLE_SUPPORT,
    )
    messages.success(request, _("The level of %(email)s was changed.") % {"email": target.email})
    return _back()


@superadmin_required
@require_POST
def team_remove(request, user_id):
    target = _other_operator(request, user_id)
    email = target.email
    try:
        deleted = operators.remove_operator(target)
    except operators.LastSuperadminError:
        messages.error(request, _("The team needs at least one active superadmin."))
        return _back()
    logger.warning("Operator %s removed the team account %s (deleted: %s)", request.user.email, email, deleted)
    if deleted:
        messages.success(request, _("%(email)s was removed from the team.") % {"email": email})
    else:
        messages.success(
            request,
            _("%(email)s left the team and is a customer account again, with its organizations and brands.")
            % {"email": email},
        )
    return _back()
