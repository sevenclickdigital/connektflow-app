"""Background tasks of the support area."""

import logging

from background_task import background

logger = logging.getLogger(__name__)

# How often queued chats whose visitor left are turned into tickets. They wait
# services.ABANDONED_AFTER first, so a few minutes late changes nothing; a
# chat converted once is closed, so an overlap cannot convert it twice.
ABANDONED_CHATS_INTERVAL_SECONDS = 5 * 60


@background(schedule=0)
def convert_abandoned_chats():
    """Turn the queue's abandoned chats into tickets (``apps.support.services.convert_abandoned``)."""
    from .services import convert_abandoned

    converted = convert_abandoned()
    if converted:
        logger.info("Turned %d abandoned chat(s) into tickets", converted)
