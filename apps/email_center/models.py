"""E-mail written by the team in Markdown: versions of the system e-mails and automatic e-mails.

Neither is tenant data: both are product content the team edits in the
administrative area (``/staff/emails/``), the same for every account.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class SystemEmailOverride(models.Model):
    """A Markdown version of one system e-mail, used instead of its code template while active.

    ``key`` is the template prefix the e-mail is built from
    (``apps.email_center.catalog``). Without an active row the e-mail keeps
    its code template, with its translations and conditional parts.
    """

    key = models.CharField(max_length=120, unique=True)
    subject = models.CharField(max_length=200)
    body_markdown = models.TextField()
    is_active = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "email_center_system_email_override"

    def __str__(self):
        return self.key


class Presence(models.TextChoices):
    """Whether something must have happened, must not have, or does not matter."""

    ANY = "any", _("Doesn't matter")
    NONE = "none", _("Hasn't yet")
    SOME = "some", _("Already has")


class AutomatedEmail(models.Model):
    """An e-mail sent on its own to account owners who match its trigger.

    The trigger is a starting point (``anchor``), a number of days after it,
    and conditions on what the person has done in the product. Each person
    receives each automatic e-mail once (``AutomatedEmailSend``).
    """

    class Anchor(models.TextChoices):
        SIGNUP = "signup", _("Sign-up")
        LAST_LOGIN = "last_login", _("Last sign-in")
        LAST_CONNECTION = "last_connection", _("Last social account connected")
        LAST_POST = "last_post", _("Last post created")
        LAST_TASK = "last_task", _("Last task created")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    subject = models.CharField(max_length=200)
    body_markdown = models.TextField()
    is_active = models.BooleanField(default=False)
    # When it was last switched on. Unless ``include_overdue``, only people
    # whose due date falls after this receive it, so switching an e-mail on
    # does not mail everyone who passed the date long ago.
    activated_at = models.DateTimeField(null=True, blank=True)
    include_overdue = models.BooleanField(default=False)

    anchor = models.CharField(max_length=20, choices=Anchor.choices, default=Anchor.SIGNUP)
    delay_days = models.PositiveSmallIntegerField(default=3)

    connections = models.CharField(max_length=4, choices=Presence.choices, default=Presence.ANY)
    posts = models.CharField(max_length=4, choices=Presence.choices, default=Presence.ANY)
    tasks = models.CharField(max_length=4, choices=Presence.choices, default=Presence.ANY)
    tools = models.CharField(max_length=4, choices=Presence.choices, default=Presence.ANY)
    # Bounds on how many of the setup steps (apps.email_center.triggers.SETUP_STEPS)
    # are done; None leaves that side open.
    setup_steps_min = models.PositiveSmallIntegerField(null=True, blank=True)
    setup_steps_max = models.PositiveSmallIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "email_center_automated_email"
        ordering = ["name"]

    def __str__(self):
        return self.name


class AutomatedEmailSend(models.Model):
    """One automatic e-mail sent to one person. The row is what keeps it to once."""

    automated_email = models.ForeignKey(AutomatedEmail, on_delete=models.CASCADE, related_name="sends")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "email_center_automated_email_send"
        constraints = [
            models.UniqueConstraint(fields=["automated_email", "user"], name="email_center_one_send_per_user"),
        ]
