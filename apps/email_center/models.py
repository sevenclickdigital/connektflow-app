"""E-mail written by the team in Markdown: versions of the system e-mails and automatic e-mails.

Neither is tenant data: both are product content the team edits in the
administrative area (``/staff/emails/``), the same for every account.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class SystemEmailOverride(models.Model):
    """The Markdown version of one system e-mail in one language, used instead of its code template while active.

    ``key`` is the template prefix the e-mail is built from
    (``apps.email_center.catalog``) and ``language`` one of
    ``settings.LANGUAGES``. Each e-mail has a row per language, created with
    the code template's text (``is_default_text``) by
    ``defaults.ensure_versions``. Without an active row for the language a
    send happens in, the e-mail keeps its code template, with its translations
    and conditional parts.
    """

    key = models.CharField(max_length=120)
    language = models.CharField(max_length=10, default="pt-br", db_default="pt-br")
    subject = models.CharField(max_length=200)
    body_markdown = models.TextField()
    is_active = models.BooleanField(default=False)
    # True while the text is the code template's, as created; saving from the
    # editor clears it and "Restore default" sets it again.
    is_default_text = models.BooleanField(default=False, db_default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "email_center_system_email_override"
        constraints = [
            models.UniqueConstraint(fields=["key", "language"], name="email_center_one_version_per_language"),
        ]

    def __str__(self):
        return f"{self.key} ({self.language})"

    @property
    def status(self) -> str:
        """``active`` (sent), ``draft`` (edited, not sent) or ``default`` (the code template's text, not sent)."""
        if self.is_active:
            return "active"
        return "default" if self.is_default_text else "draft"


class Presence(models.TextChoices):
    """Whether something must have happened, must not have, or does not matter."""

    ANY = "any", _("Doesn't matter")
    NONE = "none", _("Hasn't yet")
    SOME = "some", _("Already has")


class AutomatedEmail(models.Model):
    """An e-mail sent on its own to account owners who match its trigger.

    The trigger is a starting point (``anchor``), a number of days after it,
    and conditions on what the person has done in the product. Each person
    receives each automatic e-mail once (``AutomatedEmailSend``), in the
    language of their profile (``texts``, one per language).
    """

    class Anchor(models.TextChoices):
        SIGNUP = "signup", _("Sign-up")
        LAST_LOGIN = "last_login", _("Last sign-in")
        LAST_CONNECTION = "last_connection", _("Last social account connected")
        LAST_POST = "last_post", _("Last post created")
        LAST_TASK = "last_task", _("Last task created")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
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

    def text_for(self, language: str) -> AutomatedEmailText | None:
        """The text in ``language``, else in the product's default language, else any.

        Reads ``texts`` through the prefetch cache when the caller prefetched it.
        """
        texts = {text.language: text for text in self.texts.all()}
        return texts.get(language) or texts.get(settings.LANGUAGE_CODE) or next(iter(texts.values()), None)

    @property
    def default_text(self) -> AutomatedEmailText | None:
        return self.text_for(settings.LANGUAGE_CODE)

    @property
    def written_languages(self) -> list[str]:
        """Short labels (``PT``, ``EN``) of the languages the e-mail has a text in, in ``settings.LANGUAGES`` order."""
        written = {text.language for text in self.texts.all()}
        return [code.split("-")[0].upper() for code, _name in settings.LANGUAGES if code in written]

    def start_clock_if_switched_on(self, was_active: bool) -> None:
        """Switching on (again) restarts the clock: without ``include_overdue``,
        only people whose day comes from now on receive it."""
        if self.is_active and (not was_active or self.activated_at is None):
            self.activated_at = timezone.now()


class AutomatedEmailText(models.Model):
    """The subject and Markdown text of an automatic e-mail in one language (a code of ``settings.LANGUAGES``).

    The default language's text always exists; another language's is
    optional, and a person whose language has none receives the default one.
    """

    automated_email = models.ForeignKey(AutomatedEmail, on_delete=models.CASCADE, related_name="texts")
    language = models.CharField(max_length=10)
    subject = models.CharField(max_length=200)
    body_markdown = models.TextField()

    class Meta:
        db_table = "email_center_automated_email_text"
        constraints = [
            models.UniqueConstraint(fields=["automated_email", "language"], name="email_center_one_text_per_language"),
        ]

    def __str__(self):
        return f"{self.automated_email} ({self.language})"


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
