"""Who receives each automatic e-mail, and sending it.

The recipients are account owners: people who own at least one organization
not being deleted. What a person "has done" is read across every brand of the
organizations they own:

- connections: a social account connected;
- posts: a post created;
- tasks: a task created;
- tools: something made in Radar (a source connected, an offer saved, a
  profile watched), Automations (a rule), Ads (an ad account connected), or
  Contacts and Funnels (a contact added by hand or imported, a funnel of
  their own). Contacts the inbox records by itself and the default funnel
  every brand starts with do not count;
- setup steps: how many of the onboarding checklist items
  (``apps.onboarding.checklist``) are done in at least one brand.

An e-mail is due ``delay_days`` after its anchor (sign-up, last sign-in, last
connection, post or task). It goes once per person (``AutomatedEmailSend``),
only to who has not turned off "Tips and reminders", and, unless
``include_overdue``, only when the due moment came after it was switched on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cached_property
from typing import Any

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from .models import AutomatedEmail, AutomatedEmailSend, Presence

logger = logging.getLogger(__name__)

#: The onboarding checklist items, the "setup steps" an e-mail can count.
SETUP_STEPS = 4

#: What an automatic e-mail can put in with ``{{ name }}``.
VARIABLES = {
    "name": _("First name of the person"),
    "email": _("E-mail of the person"),
    "organization_name": _("Their organization"),
    "brand": _("Their brand"),
    "app_name": _("Product name"),
    "app_url": _("Address of the app"),
    "connect_url": _("Link to connect social accounts"),
    "compose_url": _("Link to create a post"),
    "tasks_url": _("Link to the tasks board"),
    "support_email": _("Support e-mail"),
}


@dataclass
class Activity:
    """What one account owner has done, read once per run."""

    user: Any

    @cached_property
    def workspaces(self):
        from apps.workspaces.models import Workspace

        return list(
            Workspace.objects.filter(
                organization__memberships__user=self.user,
                organization__memberships__org_role="owner",
                organization__deletion_requested_at__isnull=True,
            )
            .select_related("organization")
            .order_by("created_at")
        )

    @cached_property
    def workspace_ids(self):
        return [w.id for w in self.workspaces]

    @cached_property
    def last_connection(self) -> datetime | None:
        from apps.social_accounts.models import SocialAccount

        return SocialAccount.objects.filter(workspace_id__in=self.workspace_ids).aggregate(t=Max("connected_at"))["t"]

    @cached_property
    def last_post(self) -> datetime | None:
        from apps.composer.models import Post

        return Post.objects.filter(workspace_id__in=self.workspace_ids).aggregate(t=Max("created_at"))["t"]

    @cached_property
    def last_task(self) -> datetime | None:
        from apps.tasks.models import Task

        return Task.objects.filter(workspace_id__in=self.workspace_ids).aggregate(t=Max("created_at"))["t"]

    @cached_property
    def used_tools(self) -> bool:
        from apps.ads.models import AdsConnection
        from apps.automations.models import AutomationRule
        from apps.contacts.models import Contact
        from apps.funnels.models import Funnel
        from apps.radar.models import OpportunityConnection, SavedOffer, WatchedProfile

        ids = self.workspace_ids
        checks = (
            OpportunityConnection.objects.filter(workspace_id__in=ids),
            SavedOffer.objects.filter(workspace_id__in=ids),
            WatchedProfile.objects.filter(workspace_id__in=ids),
            AutomationRule.objects.filter(workspace_id__in=ids),
            AdsConnection.objects.filter(workspace_id__in=ids),
            Contact.objects.filter(workspace_id__in=ids, source__in=[Contact.Source.MANUAL, Contact.Source.IMPORT]),
            Funnel.objects.filter(workspace_id__in=ids, is_default=False),
        )
        return any(query.exists() for query in checks)

    @cached_property
    def setup_steps(self) -> int:
        from apps.onboarding.checklist import get_checklist_items

        done = set()
        for workspace in self.workspaces:
            done |= {item["key"] for item in get_checklist_items(workspace) if item["completed"]}
        return len(done)

    def has(self, what: str) -> bool:
        return {
            "connections": self.last_connection is not None,
            "posts": self.last_post is not None,
            "tasks": self.last_task is not None,
            "tools": self.used_tools,
        }[what]

    def anchor(self, anchor: str) -> datetime | None:
        moments: dict[str, datetime | None] = {
            AutomatedEmail.Anchor.SIGNUP.value: self.user.created_at,
            AutomatedEmail.Anchor.LAST_LOGIN.value: self.user.last_login,
            AutomatedEmail.Anchor.LAST_CONNECTION.value: self.last_connection,
            AutomatedEmail.Anchor.LAST_POST.value: self.last_post,
            AutomatedEmail.Anchor.LAST_TASK.value: self.last_task,
        }
        return moments[anchor]


def matches(email: AutomatedEmail, activity: Activity) -> bool:
    """Whether ``activity`` meets every condition of ``email``."""
    for field in ("connections", "posts", "tasks", "tools"):
        wanted = getattr(email, field)
        if wanted == Presence.ANY:
            continue
        if activity.has(field) != (wanted == Presence.SOME):
            return False
    steps = None
    if email.setup_steps_min is not None:
        steps = activity.setup_steps
        if steps < email.setup_steps_min:
            return False
    if email.setup_steps_max is not None:
        steps = activity.setup_steps if steps is None else steps
        if steps > email.setup_steps_max:
            return False
    return True


def due_at(email: AutomatedEmail, activity: Activity) -> datetime | None:
    anchor = activity.anchor(email.anchor)
    return None if anchor is None else anchor + timedelta(days=email.delay_days)


def is_due(email: AutomatedEmail, activity: Activity, now: datetime) -> bool:
    due = due_at(email, activity)
    if due is None or due > now:
        return False
    if email.include_overdue:
        return True
    return email.activated_at is not None and due >= email.activated_at


def recipients():
    """Account owners who can receive automatic e-mail: active, not staff, with a confirmed address."""
    from apps.accounts.models import User

    return (
        User.objects.filter(
            is_active=True,
            is_staff=False,
            org_memberships__org_role="owner",
            org_memberships__organization__deletion_requested_at__isnull=True,
            emailaddress__verified=True,
        )
        .exclude(email="")
        .distinct()
        .order_by("pk")
    )


def values_for(user, activity: Activity) -> dict[str, str]:
    from apps.common.emails import email_context

    app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
    workspaces = activity.workspaces
    workspace = next((w for w in workspaces if w.id == user.last_workspace_id), workspaces[0] if workspaces else None)
    common = email_context()

    def url(name):
        return f"{app_url}{reverse(name, kwargs={'workspace_id': workspace.id})}" if workspace else app_url

    return {
        "name": user.greeting_name,
        "email": user.email,
        "organization_name": workspace.organization.name if workspace else "",
        "brand": workspace.name if workspace else "",
        "app_name": str(common.get("brand_name", "")),
        "app_url": app_url,
        "connect_url": url("social_accounts:connect"),
        "compose_url": url("composer:compose"),
        "tasks_url": url("tasks:board"),
        "support_email": str(common.get("support_email", "")),
    }


def build_message(email: AutomatedEmail, user, activity: Activity):
    """The ready-to-send message of ``email`` for ``user``, with the one-click unsubscribe."""
    from django.core.mail import EmailMultiAlternatives

    from apps.notifications.models import EventType
    from apps.notifications.unsubscribe import notification_email_parts

    from .rendering import render_message

    headers, unsubscribe_context = notification_email_parts(user.id, EventType.LIFECYCLE_TIPS)
    with translation.override(settings.LANGUAGE_CODE):
        subject, text, html = render_message(
            subject=email.subject,
            body_markdown=email.body_markdown,
            values=values_for(user, activity),
            context=unsubscribe_context,
            reason_text=gettext("You're receiving this because you have an account on Connekt Flow."),
        )
    message = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
        to=[user.email],
        headers=headers,
    )
    message.attach_alternative(html, "text/html")
    return message


def _wants_tips(user) -> bool:
    from apps.notifications.engine import _resolve_channels
    from apps.notifications.models import Channel, EventType

    return str(Channel.EMAIL) in _resolve_channels(user, EventType.LIFECYCLE_TIPS)


def send_one(email: AutomatedEmail, user, activity: Activity) -> bool:
    """Send ``email`` to ``user`` once. False when it was already sent or could not go out.

    The send row is written first and is what keeps it to once; a send that
    fails removes it, so the next run tries again.
    """
    from apps.common.mail import EmailNotSentError, send_or_raise

    try:
        claim = AutomatedEmailSend.objects.create(automated_email=email, user=user)
    except IntegrityError:
        return False
    try:
        send_or_raise(build_message(email, user, activity))
    except EmailNotSentError:
        claim.delete()
        logger.warning("Automatic e-mail %s to user %s was not sent; retrying next run", email.pk, user.pk)
        return False
    except Exception:
        claim.delete()
        logger.exception("Automatic e-mail %s to user %s failed", email.pk, user.pk)
        return False
    return True


def send_due(now: datetime | None = None) -> int:
    """Send every automatic e-mail that is due. Returns how many went out."""
    now = now or timezone.now()
    emails = list(AutomatedEmail.objects.filter(is_active=True))
    if not emails:
        return 0
    sent_pairs = set(
        AutomatedEmailSend.objects.filter(automated_email__in=emails).values_list("automated_email_id", "user_id")
    )
    sent = 0
    for user in recipients().iterator():
        pending = [email for email in emails if (email.id, user.id) not in sent_pairs]
        if not pending:
            continue
        activity = Activity(user)
        wants_tips = None
        for email in pending:
            if not is_due(email, activity, now) or not matches(email, activity):
                continue
            if wants_tips is None:
                wants_tips = _wants_tips(user)
            if not wants_tips:
                break
            sent += send_one(email, user, activity)
    return sent
