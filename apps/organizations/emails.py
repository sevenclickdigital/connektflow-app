"""E-mail the organization's own flows send, built like the rest of our mail
(``apps.common.emails.build_email``) and classed as transactional."""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from apps.common.emails import build_email
from apps.common.mail import EmailNotSentError, send_or_raise, transactional

logger = logging.getLogger(__name__)


def send_deletion_scheduled(organization, user):
    """Tell the owner who asked to delete ``organization`` when it goes and where to undo it.

    The link opens the danger zone of the organization's page, where
    "Cancel deletion" is, and works until ``deletion_scheduled_for``. Sent
    after the transaction commits, in the language of the request. A
    failure to send is logged and never undoes the request: the page shows
    the same date and the same way back.
    """
    if not user.email:
        return
    app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
    restore_url = f"{app_url}{reverse('organizations:manage', kwargs={'organization_id': organization.pk})}#danger-zone"
    message = build_email(
        subject=_("%(name)s will be deleted on %(date)s")
        % {
            "name": organization.name,
            "date": date_format(timezone.localtime(organization.deletion_scheduled_for), "SHORT_DATE_FORMAT"),
        },
        template_prefix="organizations/email/deletion_scheduled",
        context={
            "greeting_name": user.greeting_name,
            "organization": organization,
            "deletion_at": organization.deletion_scheduled_for,
            "restore_url": restore_url,
        },
        to=[user.email],
        headers=transactional(),
    )

    def _send():
        try:
            send_or_raise(message)
        except EmailNotSentError:
            logger.warning("Deletion notice of organization %s was not sent", organization.pk)
        except Exception:
            logger.exception("Could not send the deletion notice of organization %s", organization.pk)

    transaction.on_commit(_send)
