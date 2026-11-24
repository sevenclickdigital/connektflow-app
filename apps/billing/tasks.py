"""Background tasks for the billing app."""

import logging

from background_task import background

logger = logging.getLogger(__name__)

# How often the courtesy-expiry sweep runs; registered on a repeating
# schedule by apps.billing.apps.BillingConfig.
#
# Daily, not hourly: the deadline is a *date*
# (``WorkspaceSubscription.courtesy_expires_on``), so the finest
# distinction the data can make is one day. Running more often would only
# re-ask a question whose answer cannot have changed.
COURTESY_EXPIRY_INTERVAL_SECONDS = 24 * 60 * 60  # daily


@background(schedule=0)
def expire_courtesies():
    """Return brands whose free period has ended to being billed.

    Only billing state moves — see ``services.expire_due_courtesies``.
    Nothing is deleted and no access is withdrawn: an expired courtesy
    means the brand starts being charged, not that it stops working.

    Logged at ``warning`` when it actually expires something, because
    somebody is about to be charged who was not being charged yesterday
    and that should be findable in the logs without a database query.
    """
    from apps.billing import services

    expired = services.expire_due_courtesies()
    if expired:
        logger.warning("Courtesy expired for %d brand(s); they are billable again", expired)
    return expired


@background(schedule=0)
def end_cancelled_subscriptions():
    """Close the plans and brands the customer cancelled, on the day their paid period ends.

    Daily, like the courtesy sweep and for the same reason: the deadline is
    a date (``cancels_on``). See ``services.end_cancelled_subscriptions``;
    running it twice changes nothing the second time.
    """
    from apps.billing import services

    closed = services.end_cancelled_subscriptions()
    if closed:
        logger.warning("%d cancelled brand(s) reached the end of their paid period and were closed", closed)
    return closed


@background(schedule=0)
def remove_expired_archives():
    """Warn the archived organizations close to their removal, and remove the ones whose notice has run.

    Daily: the retention is counted in months. See
    ``services.remove_expired_archives``; a second run the same day finds
    the warned ones warned and the removed ones gone.
    """
    from apps.billing import services

    warned, removed = services.remove_expired_archives()
    if warned or removed:
        logger.warning("Archived organizations: %d warned of their removal, %d removed", warned, removed)
    return warned, removed
