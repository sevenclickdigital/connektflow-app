"""Background tasks for the accounts app."""

import logging

from background_task import background
from django.core.management import call_command

logger = logging.getLogger(__name__)

# How often the recurring expired-session purge runs; registered on a repeating
# schedule by apps.accounts.apps.AccountsConfig. Replaces the VPS-only
# docker-compose ``maintenance`` loop so it runs on every deploy target.
SESSION_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60  # daily


@background(schedule=0)
def clear_expired_sessions():
    """Delete expired Django sessions (wraps the ``clearsessions`` command)."""
    call_command("clearsessions")
    logger.info("Cleared expired sessions")


@background(schedule=0)
def complete_address_from_cep(user_id):
    """Fill the rest of a profile's address from its CEP (``address.complete_address``).

    Queued when the checkout's card form leaves a CEP in an empty profile.
    Safe to run again: it only fills what is still empty, and a profile the
    person has completed since is left alone. A failure of the lookup
    service is raised, so the task is tried again.
    """
    from . import address
    from .models import User

    user = User.objects.filter(pk=user_id).first()
    if user is not None:
        address.complete_address(user)
