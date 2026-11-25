"""Housekeeping for the shared tables in this app."""

import logging
import time
from datetime import timedelta

from background_task import background
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# Daily is often enough: the rows are tiny and only a week of them is kept.
COUNTER_PURGE_INTERVAL_SECONDS = 24 * 60 * 60


@background(schedule=0)
def purge_email_counters():
    """Drop email budget buckets whose period is long past, and the sent-mail history past its retention.

    One row per recipient per hour accumulates quickly and nothing else ever
    deletes them. Exceptions are swallowed deliberately: django-background-tasks
    backs a raising task off exponentially and deletes it after 25 attempts, and
    losing the schedule over a housekeeping failure is worse than the rows.
    """
    from .mail import purge_expired_counters, purge_old_email_logs

    try:
        deleted = purge_expired_counters()
        forgotten = purge_old_email_logs()
    except Exception:
        logger.exception("Email counter purge failed")
        return

    if deleted:
        logger.info("Purged %d expired email counter row(s)", deleted)
    if forgotten:
        logger.info("Purged %d row(s) of the sent e-mail history past its retention", forgotten)


COMPLETED_TASK_PURGE_INTERVAL_SECONDS = 24 * 60 * 60
# A week tells whether a job ran; a failure is kept longer, to be looked into.
COMPLETED_TASK_RETENTION = timedelta(days=7)
FAILED_TASK_RETENTION = timedelta(days=30)
_PURGE_BATCH = 5000
# What one run may spend deleting; a backlog larger than that is finished by
# the next runs rather than holding the lane.
_PURGE_BUDGET_SECONDS = 120


@background(schedule=0)
def purge_completed_tasks():
    """Drop the queue library's record of finished tasks past its retention.

    The library writes one ``CompletedTask`` row per run of every task,
    successful or not, and never deletes any: thousands a day. Exceptions are
    swallowed for the reason given in ``purge_email_counters``.
    """
    from background_task.models import CompletedTask

    now = timezone.now()
    stale = CompletedTask.objects.filter(
        Q(failed_at__isnull=True, run_at__lt=now - COMPLETED_TASK_RETENTION)
        | Q(failed_at__isnull=False, run_at__lt=now - FAILED_TASK_RETENTION)
    )
    deadline = time.monotonic() + _PURGE_BUDGET_SECONDS
    deleted = 0
    try:
        while time.monotonic() < deadline:
            batch = list(stale.values_list("pk", flat=True)[:_PURGE_BATCH])
            if not batch:
                break
            deleted += CompletedTask.objects.filter(pk__in=batch).delete()[0]
    except Exception:
        logger.exception("Completed task purge failed")
        return
    if deleted:
        logger.info("Purged %d finished task record(s) past their retention", deleted)
