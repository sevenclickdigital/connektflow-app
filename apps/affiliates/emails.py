"""E-mail the affiliate programme sends to the partner.

Built like the rest of our mail (``apps.common.emails.build_email``) and
classed as transactional: it answers something the team did to the person's
account. Each is sent at once and answered with its outcome, so the
operator's screen can say when the person was not told, and written in the
language of the affiliate's profile, which may not be the operator's. The
team's notice goes out in the product's default language.
"""

from __future__ import annotations

import logging
import mimetypes

from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _

from apps.affiliates import services
from apps.billing.templatetags.billing_extras import brl
from apps.common.emails import build_email, in_language_of
from apps.common.mail import EmailNotSentError, send_or_raise, transactional

logger = logging.getLogger(__name__)


def send_affiliate_added(affiliate) -> bool:
    """Tell the account an operator just made an affiliate: their link and their terms. True when it was sent."""
    return _send(affiliate, "affiliate_added", lambda: _("You are now an affiliate"))


def send_terms_changed(affiliate) -> bool:
    """Tell the affiliate what they earn and how they are paid from now on. True when it was sent."""
    return _send(affiliate, "terms_changed", lambda: _("Your affiliate terms have changed"))


def send_payout_requested(payout) -> bool:
    """Confirm to the affiliate the withdrawal they asked for: amount, Pix key and deadline. True when it was sent."""
    return _send(
        payout.affiliate,
        "payout_requested",
        lambda: _("We received your withdrawal request of R$ %(amount)s") % {"amount": brl(payout.amount_cents)},
        payout=payout,
        deadline=services.payout_deadline(payout),
        payout_days=services.PAYOUT_DEADLINE_DAYS,
    )


def send_payout_paid(payout) -> bool:
    """Tell the affiliate their withdrawal was transferred, with the receipt attached. True when it was sent."""
    attachment = None
    if payout.receipt:
        try:
            with payout.receipt.open("rb") as handle:
                content = handle.read()
        except Exception:
            logger.exception("Could not read the receipt of payout %s", payout.pk)
            return False
        name = payout.receipt.name.rsplit("/", 1)[-1]
        attachment = (name, content, mimetypes.guess_type(name)[0] or "application/octet-stream")
    return _send(
        payout.affiliate,
        "payout_paid",
        lambda: _("Your withdrawal of R$ %(amount)s is in your account") % {"amount": brl(payout.amount_cents)},
        payout=payout,
        attachment=attachment,
    )


def send_payout_requested_to_team(payout, *, review_url) -> bool:
    """Tell the superadmins an affiliate asked for a withdrawal. True when it was sent."""
    recipients = list(
        get_user_model()
        .objects.filter(is_staff=True, is_superuser=True, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    )
    if not recipients:
        return False
    with translation.override(settings.LANGUAGE_CODE):
        message = build_email(
            subject=_("%(name)s asked for a withdrawal of R$ %(amount)s")
            % {"name": payout.affiliate.name, "amount": brl(payout.amount_cents)},
            template_prefix="affiliates/email/payout_requested_team",
            context={
                "payout": payout,
                "deadline": services.payout_deadline(payout),
                "review_url": review_url,
            },
            to=recipients,
            headers=transactional(),
        )
    return _deliver(message, "payout_requested_team", payout.affiliate)


def _send(affiliate, template, subject, *, attachment=None, **extra) -> bool:
    user = affiliate.user
    if user is None or not user.email:
        return False
    app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
    with in_language_of(user):
        message = build_email(
            subject=subject(),
            template_prefix=f"affiliates/email/{template}",
            context={
                "greeting_name": user.greeting_name,
                "affiliate": affiliate,
                "link": f"{app_url}{reverse('affiliate_link', kwargs={'slug': affiliate.slug})}",
                "panel_url": f"{app_url}{reverse('affiliates:panel')}",
                "withdrawals_url": f"{app_url}{reverse('affiliates:panel')}?tab=withdrawals",
                "hold_days": services.COMMISSION_HOLD_DAYS,
                **extra,
            },
            to=[user.email],
            headers=transactional(),
        )
    if attachment is not None:
        message.attach(*attachment)
    return _deliver(message, template, affiliate)


def _deliver(message, template, affiliate) -> bool:
    try:
        send_or_raise(message)
    except EmailNotSentError:
        logger.warning("The %s e-mail of affiliate %s was not sent", template, affiliate.slug)
        return False
    except Exception:
        logger.exception("Could not send the %s e-mail of affiliate %s", template, affiliate.slug)
        return False
    return True
