import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.common.managers import WorkspaceScopedManager


def _generate_connection_token():
    return secrets.token_urlsafe(32)


class ConnectionLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="connection_links",
    )
    token = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        default=_generate_connection_token,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_connection_links",
    )
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "onboarding_connection_link"

    def __str__(self):
        return f"ConnectionLink for {self.workspace.name} (expires {self.expires_at})"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_active(self):
        return not self.is_expired and not self.is_revoked


class ConnectionLinkUsage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection_link = models.ForeignKey(
        ConnectionLink,
        on_delete=models.CASCADE,
        related_name="usages",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="connection_link_usages",
    )
    connected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "onboarding_connection_link_usage"
        unique_together = [("connection_link", "social_account")]

    def __str__(self):
        return f"{self.social_account} via {self.connection_link}"


class OnboardingChecklist(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="onboarding_checklists",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="onboarding_checklists",
    )
    is_dismissed = models.BooleanField(default=False)
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "onboarding_checklist"
        unique_together = [("user", "workspace")]

    def __str__(self):
        status = "dismissed" if self.is_dismissed else "active"
        return f"Checklist for {self.user} in {self.workspace} ({status})"


class PageHelpVideo(models.Model):
    """The YouTube video behind a page banner's "See how it works" button.

    Product-wide, not per workspace: the operator sets one link per page in
    the administrative area (onboarding.staff_views) and every brand sees it.
    A page without a row has no video, and its banner shows no button.
    """

    class Page(models.TextChoices):
        DASHBOARD = "dashboard", pgettext_lazy("menu", "Dashboard")
        CALENDAR = "calendar", _("Calendar")
        TASKS = "tasks", _("Task board")
        IDEAS = "ideas", _("Ideas")
        TEMPLATES = "templates", _("Templates")
        DATES = "dates", _("Special dates")
        INBOX = "inbox", pgettext_lazy("menu", "Inbox")
        AUTOMATIONS = "automations", pgettext_lazy("menu", "Automation")
        CONTACTS = "contacts", pgettext_lazy("menu", "Contacts")
        ANALYTICS = "analytics", _("Reports")
        ACCOUNTS = "accounts", _("Social Accounts")
        MEDIA = "media", _("Gallery")
        RADAR = "radar", _("Radar")
        SHOPEE = "shopee", _("Shopee Affiliates")
        INSTAGRAM_RESEARCH = "instagram_research", _("Instagram research")

    page = models.CharField(max_length=20, choices=Page.choices, unique=True)
    video_url = models.URLField(max_length=300)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "onboarding_page_help_video"

    def __str__(self):
        return f"{self.page}: {self.video_url}"
