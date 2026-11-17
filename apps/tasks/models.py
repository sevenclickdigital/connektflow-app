"""The team's task board: columns, task cards and their checklists.

One board per workspace. A task can stand alone or come from an idea's art
request, in which case its checklist holds one item per creative format, each
carrying the exact size the designer has to deliver and, once delivered, the
file itself.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.managers import WorkspaceScopedManager


class TaskColumn(models.Model):
    """A column of the board. Exactly the columns flagged ``is_done`` count as finished."""

    class Tone(models.TextChoices):
        """The column's colour, drawn by the shared ``.kanban-tone-*`` classes (styles.css)."""

        BRAND = "brand", _("Purple")
        INFO = "info", _("Blue")
        WARNING = "warning", _("Yellow")
        SUCCESS = "success", _("Green")
        ERROR = "error", _("Red")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="task_columns")
    name = models.CharField(max_length=100)
    position = models.PositiveIntegerField(default=0)
    tone = models.CharField(max_length=10, choices=Tone.choices, default=Tone.BRAND, db_default=Tone.BRAND)
    is_done = models.BooleanField(default=False)
    is_review = models.BooleanField(
        default=False,
        help_text="Where a task goes on its own once every art on its checklist is delivered.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "tasks_column"
        ordering = ["position", "created_at"]

    def __str__(self):
        return self.name


class Task(models.Model):
    class Priority(models.TextChoices):
        LOW = "low", _("Low")
        MEDIUM = "medium", _("Medium")
        HIGH = "high", _("High")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="tasks")
    column = models.ForeignKey(TaskColumn, on_delete=models.PROTECT, related_name="tasks")
    position = models.PositiveIntegerField(default=0)

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    # The text the post goes out with. Kept apart from ``description``, which on
    # an art request is the briefing for the designers.
    post_caption = models.TextField(blank=True, default="")
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    due_date = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_tasks"
    )
    assignees = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="assigned_tasks")

    idea = models.ForeignKey("composer.Idea", on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks")
    post = models.ForeignKey("composer.Post", on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks")
    calendar_event = models.ForeignKey(
        "calendar.CustomCalendarEvent", on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )

    completed_at = models.DateTimeField(null=True, blank=True)
    due_reminder_sent_on = models.DateField(
        null=True,
        blank=True,
        help_text="Due date the reminder was sent for; a new due date gets a new reminder.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "tasks_task"
        ordering = ["position", "-created_at"]
        indexes = [models.Index(fields=["workspace", "due_date"])]

    def __str__(self):
        return self.title[:80]


class TaskChecklistItem(models.Model):
    """One to-do of a task. Art items also name the format and size to deliver."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="checklist")
    text = models.CharField(max_length=255)
    position = models.PositiveIntegerField(default=0)

    is_done = models.BooleanField(default=False)
    done_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)

    # Set only on items created by an art request (see apps/tasks/creative_formats.py).
    format_key = models.CharField(max_length=60, blank=True, default="")
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    media_asset = models.ForeignKey(
        "media_library.MediaAsset", on_delete=models.SET_NULL, null=True, blank=True, related_name="task_items"
    )

    class Meta:
        db_table = "tasks_checklist_item"
        ordering = ["position", "id"]

    def __str__(self):
        return self.text

    @property
    def is_art(self):
        return bool(self.format_key)


class TaskSettings(models.Model):
    """Per-workspace defaults for art requests."""

    workspace = models.OneToOneField(
        "workspaces.Workspace", on_delete=models.CASCADE, primary_key=True, related_name="task_settings"
    )
    art_assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Who an art request is assigned to when the requester picks nobody.",
    )
    art_lead_days = models.PositiveSmallIntegerField(
        default=3, help_text="How many days before the idea's date the arts are due."
    )

    class Meta:
        db_table = "tasks_settings"
