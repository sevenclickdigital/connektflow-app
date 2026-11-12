"""Background jobs of the task board."""

import logging

from background_task import background

logger = logging.getLogger(__name__)

# Hourly rather than daily: a reminder is sent once per due date anyway, and an
# hourly sweep doesn't depend on the worker being up at one particular time.
DUE_REMINDER_INTERVAL_SECONDS = 60 * 60


@background(schedule=0)
def send_task_due_reminders():
    from apps.tasks.services import send_due_reminders

    sent = send_due_reminders()
    if sent:
        logger.info("Sent %d task due reminders", sent)
