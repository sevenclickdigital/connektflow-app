"""E-mail of the support area, built like the rest of our mail (``apps.common.emails.build_email``).

The team's reply goes to the requester in the language of their profile; the
notice of a new ticket goes to every active team account in the product's
default language. Both are listed in ``apps.email_center.catalog``, so the
team can rewrite them. A failure to send is logged and never undoes the
reply or the ticket: both stay on screen.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _

from apps.common.emails import build_email, in_language_of
from apps.common.mail import EmailNotSentError, send_or_raise, transactional

logger = logging.getLogger(__name__)


def _app_url() -> str:
    return getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")


def send_ticket_reply(ticket, message) -> bool:
    user = ticket.requester
    if not user.email:
        return False
    with in_language_of(user):
        email = build_email(
            subject=_("New reply to your ticket #%(number)s") % {"number": ticket.pk},
            template_prefix="support/email/ticket_reply",
            context={
                "greeting_name": user.greeting_name,
                "ticket": ticket,
                "reply": message.body,
                "agent_name": message.author.greeting_name if message.author else "",
                "ticket_url": f"{_app_url()}{reverse('support:ticket', kwargs={'ticket_id': ticket.pk})}",
            },
            to=[user.email],
            headers=transactional(),
        )
    return _deliver(email, "ticket_reply", ticket)


def send_ticket_opened_to_team(ticket) -> bool:
    recipients = list(
        get_user_model().objects.filter(is_staff=True, is_active=True).exclude(email="").values_list("email", flat=True)
    )
    if not recipients:
        return False
    first = ticket.messages.first()
    with translation.override(settings.LANGUAGE_CODE):
        email = build_email(
            subject=_("New ticket #%(number)s: %(subject)s") % {"number": ticket.pk, "subject": ticket.subject},
            template_prefix="support/email/ticket_opened_team",
            context={
                "ticket": ticket,
                "requester_name": ticket.requester.display_name,
                "requester_email": ticket.requester.email,
                "kind": ticket.get_kind_display(),
                "message": first.body if first else "",
                "review_url": f"{_app_url()}{reverse('support_staff:ticket', kwargs={'ticket_id': ticket.pk})}",
            },
            to=recipients,
            headers=transactional(),
        )
    return _deliver(email, "ticket_opened_team", ticket)


def _deliver(email, name, ticket) -> bool:
    try:
        send_or_raise(email)
    except EmailNotSentError:
        logger.warning("Support e-mail %s of ticket %s was not sent", name, ticket.pk)
        return False
    return True
