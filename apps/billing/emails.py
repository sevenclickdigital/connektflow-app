"""E-mail the billing flow sends to the customer.

Built like the rest of our mail (``apps.common.emails.build_email``) and
classed as transactional: someone who has just paid is waiting for it.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.common.emails import build_email, in_language_of
from apps.common.mail import EmailNotSentError, send_or_raise, transactional
from apps.members.models import OrgMembership

logger = logging.getLogger(__name__)


def _payer(organization):
    """Who billing mail is for: ``(owner membership or None, address)``.

    The organization's billing address, else its owner's own.
    """
    owner = (
        OrgMembership.objects.filter(organization=organization, org_role=OrgMembership.OrgRole.OWNER)
        .select_related("user")
        .order_by("invited_at")
        .first()
    )
    return owner, organization.billing_email or (owner.user.email if owner else "")


def send_payment_confirmed(organization, *, plan=None, workspace=None):
    """Congratulate the payer once a payment has put the plan (or one brand) in use.

    Called where the status turns active from anything else: the first
    payment, and a payment that recovers an overdue plan. A renewal of a
    plan already active sends nothing; the gateway's own receipt covers it.

    Goes to the organization's billing address, else to its owner, after
    the transaction commits, so a payment that rolls back congratulates
    nobody. A failure to send is logged and never undoes the payment.
    Written in the language of the owner's profile: the gateway's webhook,
    which is where most payments are confirmed, carries no language of the
    person.
    """
    owner, recipient = _payer(organization)
    if not recipient:
        return

    def _send():
        app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
        with in_language_of(owner.user if owner else recipient):
            message = build_email(
                subject=_("Payment confirmed: your plan is active"),
                template_prefix="billing/email/payment_confirmed",
                context={
                    "greeting_name": owner.user.greeting_name if owner else "",
                    "paid_for": workspace.name if workspace is not None else organization.name,
                    "plan": plan,
                    "app_url": app_url,
                    "billing_url": f"{app_url}{reverse('billing_account:overview')}",
                },
                to=[recipient],
                headers=transactional(),
            )
        try:
            send_or_raise(message)
        except EmailNotSentError:
            logger.warning("Payment confirmation to organization %s was not sent", organization.pk)
        except Exception:
            logger.exception("Could not send the payment confirmation to organization %s", organization.pk)

    transaction.on_commit(_send)


def send_archive_removal_warning(organization, removal_at):
    """Tell the payer that the archived organization is removed on ``removal_at``. True when it was sent.

    Sent at once and answered with its outcome, because the removal waits
    for this notice (``services.remove_expired_archives``): one that did
    not leave must not start the countdown.
    """
    owner, recipient = _payer(organization)
    if not recipient:
        return False
    app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
    with in_language_of(owner.user if owner else recipient):
        message = build_email(
            subject=_("Your data on %(name)s will be removed soon") % {"name": organization.name},
            template_prefix="billing/email/archive_removal_warning",
            context={
                "greeting_name": owner.user.greeting_name if owner else "",
                "organization": organization,
                "removal_at": removal_at,
                "billing_url": f"{app_url}{reverse('billing_account:overview')}",
            },
            to=[recipient],
            headers=transactional(),
        )
    try:
        send_or_raise(message)
    except EmailNotSentError:
        logger.warning("Removal warning to archived organization %s was not sent", organization.pk)
        return False
    except Exception:
        logger.exception("Could not send the removal warning to archived organization %s", organization.pk)
        return False
    return True
