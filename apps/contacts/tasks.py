"""Background tasks for contacts."""

import logging

from background_task import background

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 24 * 60 * 60


@background(schedule=0)
def refresh_contacts():
    """Daily: contacts for recent inbox senders, recounted engagement, decay of the quiet."""
    from apps.contacts.services import refresh_all

    read = refresh_all()
    logger.info("Contact refresh read %s inbox senders", read)
