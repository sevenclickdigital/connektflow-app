"""The support area: frequent questions, tickets and the live chat.

None of it is tenant data in the usual sense. FAQ entries are product content
the team writes in the administrative area. A ticket or a chat belongs to the
person who opened it (``requester`` / ``visitor``), who is the only customer
that sees it; the organization and brand they were in are kept as context
for the team, not as an access rule. The team reads every one of them under
``/staff/support/`` (``staff_required``).

What the team writes for itself never reaches the customer: a ticket's
internal notes (``TicketMessage.is_internal``) and everything about the
visitor's browser and pages (``Chat`` device fields, ``ChatPageVisit``).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy


class FaqEntry(models.Model):
    """One frequent question and its answer (Markdown), grouped by ``topic`` in the help centre."""

    topic = models.CharField(max_length=80)
    question = models.CharField(max_length=200)
    answer_markdown = models.TextField()
    position = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "support_faq_entry"
        ordering = ["topic", "position", "id"]

    def __str__(self):
        return self.question


class Ticket(models.Model):
    """A request for help, kept until the team resolves it. Its id is the number the customer sees (#123)."""

    class Kind(models.TextChoices):
        SUPPORT = "support", _("Help using the platform")
        MAINTENANCE = "maintenance", _("Maintenance")
        INFORMATION = "information", _("Information")
        BUG = "bug", _("Report a bug")

    class Status(models.TextChoices):
        OPEN = "open", pgettext_lazy("ticket status", "Open")
        IN_PROGRESS = "in_progress", pgettext_lazy("ticket status", "In progress")
        WAITING_CUSTOMER = "waiting_customer", pgettext_lazy("ticket status", "Waiting for your reply")
        RESOLVED = "resolved", pgettext_lazy("ticket status", "Resolved")

    class Source(models.TextChoices):
        FORM = "form", pgettext_lazy("ticket source", "Form")
        CHAT = "chat", pgettext_lazy("ticket source", "Live chat")

    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="support_tickets")
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.SUPPORT)
    subject = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.FORM)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_support_tickets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Moves with every message either side writes; the lists are ordered by it.
    last_activity_at = models.DateTimeField(auto_now_add=True)
    # When the requester last opened the ticket; a team reply after it is "new" to them.
    requester_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "support_ticket"
        ordering = ["-last_activity_at"]
        indexes = [
            models.Index(fields=["status", "-last_activity_at"], name="idx_ticket_status_activity"),
            models.Index(fields=["requester", "-last_activity_at"], name="idx_ticket_requester_activity"),
        ]

    def __str__(self):
        return f"#{self.pk} {self.subject}"

    @property
    def is_resolved(self) -> bool:
        return self.status == self.Status.RESOLVED


class TicketMessage(models.Model):
    """One message of a ticket's conversation. ``is_internal`` ones are team notes the requester never sees."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    from_team = models.BooleanField(default=False)
    is_internal = models.BooleanField(default=False)
    body = models.TextField()
    # Private: served by the support views after the access check, never under /media/ (config/urls.py).
    attachment = models.ImageField(upload_to="support_attachments/%Y/%m/", blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "support_ticket_message"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"Message on ticket {self.ticket_id}"


class Chat(models.Model):
    """A live conversation between a customer and the team.

    ``queued`` until an agent takes it from the queue, ``active`` while that
    agent answers, ``closed`` when either side ends it or it becomes a ticket
    (``ticket``). A person has at most one chat that is not closed.

    The device fields and the current page come from the visitor's own
    pages (``services.record_visit``) and are shown to the team only.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", pgettext_lazy("chat status", "In the queue")
        ACTIVE = "active", pgettext_lazy("chat status", "In progress")
        CLOSED = "closed", pgettext_lazy("chat status", "Closed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    visitor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="support_chats")
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="support_chats_taken"
    )
    ticket = models.ForeignKey(Ticket, on_delete=models.SET_NULL, null=True, blank=True, related_name="chats")
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    browser = models.CharField(max_length=60, blank=True, default="")
    operating_system = models.CharField(max_length=60, blank=True, default="")
    device = models.CharField(max_length=20, blank=True, default="")
    screen = models.CharField(max_length=20, blank=True, default="")
    browser_language = models.CharField(max_length=20, blank=True, default="")
    user_agent = models.CharField(max_length=400, blank=True, default="")
    current_path = models.CharField(max_length=500, blank=True, default="")
    current_title = models.CharField(max_length=200, blank=True, default="")

    # Each side's last poll: whether they are still there, and what they have read.
    visitor_seen_at = models.DateTimeField(null=True, blank=True)
    agent_seen_at = models.DateTimeField(null=True, blank=True)
    # When each side last typed in the message box (services.TYPING_WINDOW); cleared when they send.
    visitor_typing_at = models.DateTimeField(null=True, blank=True)
    agent_typing_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "support_chat"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["status", "created_at"], name="idx_chat_status_created")]
        constraints = [
            models.UniqueConstraint(
                fields=["visitor"], condition=~Q(status="closed"), name="support_one_open_chat_per_visitor"
            )
        ]

    def __str__(self):
        return f"Chat {self.pk} ({self.status})"


class ChatMessage(models.Model):
    """One message of a chat. Its id orders the conversation and is what the pollers ask "after"."""

    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    from_team = models.BooleanField(default=False)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "support_chat_message"
        ordering = ["id"]


class ChatPageVisit(models.Model):
    """A page the visitor opened while their chat was open. Shown to the team only."""

    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="visits")
    path = models.CharField(max_length=500)
    title = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "support_chat_page_visit"
        ordering = ["-created_at", "-id"]


class AgentPresence(models.Model):
    """When a team member last had the chat console open: who counts as online for the widget."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True, related_name="support_presence"
    )
    last_seen_at = models.DateTimeField()

    class Meta:
        db_table = "support_agent_presence"
