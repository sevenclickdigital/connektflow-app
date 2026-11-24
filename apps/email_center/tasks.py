"""Background tasks of the e-mail centre."""

import logging

from background_task import background

logger = logging.getLogger(__name__)

# How often due automatic e-mails are looked for. Delays are counted in days,
# so an hour late is invisible; the run is idempotent (one send row per person
# and e-mail), so an overlap cannot send twice.
AUTOMATED_EMAILS_INTERVAL_SECONDS = 60 * 60


@background(schedule=0)
def send_automated_emails():
    """Send every automatic e-mail that is due (``apps.email_center.triggers.send_due``)."""
    from .triggers import send_due

    sent = send_due()
    if sent:
        logger.info("Sent %d automatic e-mail(s)", sent)
