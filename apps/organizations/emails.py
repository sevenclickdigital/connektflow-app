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


# Brands named one by one in the notice; past this the rest are counted.
DELETION_NOTICE_MAX_BRANDS = 10


def send_deletion_scheduled(organization, user, *, removed_youtube=()):
    """Tell the owner who asked to delete ``organization`` what goes, when, and where to undo it.

    Names the brands that go with it. ``removed_youtube`` are the channels
    already disconnected by the request (``youtube_compliance.purge_organization``),
    the only connections removed before the date; the other connected
    networks keep working until then and are listed as such. The link opens
    the danger zone of the organization's page, where "Cancel deletion" is,
    and works until ``deletion_scheduled_for``. Sent after the transaction
    commits, in the language of the request. A failure to send is logged and
    never undoes the request: the page shows the same date and the same way
    back.
    """
    from apps.credentials.models import PlatformCredential
    from apps.social_accounts.models import SocialAccount

    if not user.email:
        return
    brand_names = list(organization.workspaces.order_by("name").values_list("name", flat=True))
    shown_brands = brand_names[:DELETION_NOTICE_MAX_BRANDS]
    remaining_networks = sorted(
        {
            str(PlatformCredential.Platform(platform).label).split(" (")[0]
            for platform in SocialAccount.objects.filter(workspace__organization=organization)
            .values_list("platform", flat=True)
            .distinct()
        }
    )
    app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
    restore_url = f"{app_url}{reverse('organizations:manage', kwargs={'organization_id': organization.pk})}#danger-zone"
    message = build_email(
        subject=_("The organization %(name)s will be deleted on %(date)s")
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
            "brand_names": shown_brands,
            "more_brands": len(brand_names) - len(shown_brands),
            "removed_youtube": list(removed_youtube),
            "remaining_networks": remaining_networks,
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
