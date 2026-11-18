"""Content Calendar models (F-2.3) - scheduling, time slots, queues, and events."""

import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.managers import WorkspaceScopedManager
from apps.common.validators import validate_hex_color


class PostingSlot(models.Model):
    """Recurring time slot for a social account.

    Defines default publishing times (e.g., Mon/Wed/Fri at 9 AM)
    used for queue-based scheduling.
    """

    class DayOfWeek(models.IntegerChoices):
        MONDAY = 0, _("Monday")
        TUESDAY = 1, _("Tuesday")
        WEDNESDAY = 2, _("Wednesday")
        THURSDAY = 3, _("Thursday")
        FRIDAY = 4, _("Friday")
        SATURDAY = 5, _("Saturday")
        SUNDAY = 6, _("Sunday")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="posting_slots",
    )
    day_of_week = models.IntegerField(choices=DayOfWeek.choices)
    time = models.TimeField(help_text="Posting time (in workspace timezone).")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "calendar_posting_slot"
        ordering = ["day_of_week", "time"]
        unique_together = [("social_account", "day_of_week", "time")]

    def __str__(self):
        return f"{self.get_day_of_week_display()} @ {self.time.strftime('%H:%M')} ({self.social_account})"

    @property
    def day_name(self):
        return self.get_day_of_week_display()


class Queue(models.Model):
    """A named publishing queue that maps posts to time slots.

    Each queue is tied to a workspace and optionally to a content category
    and social account. Posts added to a queue are auto-assigned to the
    next available PostingSlot datetime.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="queues",
    )
    name = models.CharField(max_length=100)
    category = models.ForeignKey(
        "composer.ContentCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="queues",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="queues",
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "calendar_queue"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.social_account})"


class QueueEntry(models.Model):
    """A post's position within a queue, with its assigned publish slot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    queue = models.ForeignKey(
        Queue,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    post = models.ForeignKey(
        "composer.Post",
        on_delete=models.CASCADE,
        related_name="queue_entries",
    )
    position = models.PositiveIntegerField(default=0)
    assigned_slot_datetime = models.DateTimeField(
        null=True,
        blank=True,
        help_text="The computed publish datetime from the queue's posting slots.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "calendar_queue_entry"
        ordering = ["position"]
        unique_together = [("queue", "post")]

    def __str__(self):
        return f"QueueEntry(pos={self.position}): {self.post_id} in {self.queue.name}"


class RecurrenceRule(models.Model):
    """Defines how a post recurs (daily, weekly, monthly).

    The background task generates individual Post records for each
    recurrence up to 90 days ahead.
    """

    class Frequency(models.TextChoices):
        DAILY = "daily", _("Daily")
        WEEKLY = "weekly", _("Weekly")
        MONTHLY = "monthly", _("Monthly")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.OneToOneField(
        "composer.Post",
        on_delete=models.CASCADE,
        related_name="recurrence_rule",
    )
    frequency = models.CharField(max_length=10, choices=Frequency.choices)
    interval = models.PositiveIntegerField(
        default=1,
        help_text="Repeat every N frequency units (e.g., every 2 weeks).",
    )
    end_date = models.DateField(
        null=True,
        blank=True,
        help_text="Stop generating recurrences after this date. Null = indefinite.",
    )
    last_generated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of last recurrence generation run.",
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "calendar_recurrence_rule"

    def __str__(self):
        return f"RecurrenceRule({self.frequency} every {self.interval}) for {self.post_id}"


class CustomCalendarEvent(models.Model):
    """Custom event on the workspace calendar (e.g., product launch, campaign start).

    Displayed as full-width colored bars spanning start_date to end_date. A
    repeating event keeps its first occurrence in start/end and is expanded
    per visible period by :meth:`occurrences`; no row is stored per repetition.
    """

    class Recurrence(models.TextChoices):
        NONE = "none", _("Does not repeat")
        MONTHLY = "monthly", _("Every month")
        YEARLY = "yearly", _("Every year")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="calendar_events",
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    start_date = models.DateField()
    end_date = models.DateField()
    color = models.CharField(
        max_length=7,
        default="#3B82F6",
        help_text="Hex color for the event bar.",
        validators=[validate_hex_color],
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    recurrence = models.CharField(
        max_length=10, choices=Recurrence.choices, default=Recurrence.NONE, db_default=Recurrence.NONE
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "calendar_custom_event"
        ordering = ["start_date"]

    def __str__(self):
        return f"{self.title} ({self.start_date} – {self.end_date})"

    def occurrences(self, start, end) -> list[tuple]:
        """``(first_day, last_day)`` of each occurrence that overlaps ``start``–``end``.

        A monthly event on the 31st falls on the last day of shorter months; a
        yearly one on 29 February falls on the 28th in common years.
        """
        length = self.end_date - self.start_date
        if self.recurrence == self.Recurrence.NONE:
            return [(self.start_date, self.end_date)] if self.start_date <= end and self.end_date >= start else []

        step = 1 if self.recurrence == self.Recurrence.MONTHLY else 12
        found = []
        # Jump close to ``start``, a year early so an occurrence that began
        # before it but runs into it is still found.
        months_from_first = (start.year - self.start_date.year) * 12 + start.month - self.start_date.month
        index = max(0, (months_from_first - 12) // step)
        while True:
            first = _shift_months(self.start_date, index * step)
            if first > end:
                return found
            if first + length >= start:
                found.append((first, first + length))
            index += 1


def _shift_months(day, months):
    import calendar as _calendar

    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, _calendar.monthrange(year, month)[1]))


class DateSegment(models.Model):
    """A line of business whose special dates a brand may follow (restaurants,
    beauty, health…). Kept by the operator in the administrative area."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    emoji = models.CharField(max_length=8, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "calendar_date_segment"
        ordering = ["name"]

    def __str__(self):
        return self.name


class SpecialDate(models.Model):
    """A special date kept by the operator, on the same day every year.

    Without a segment it is global: every brand sees it, like the built-in
    holidays in ``holidays.py``. With one it is optional: shown only to the
    brands that chose that segment.

    ``description``, ``suggestions`` (one content idea per line, as ``title``
    or ``title | brief``), ``tags``
    (comma-separated) and ``image`` fill the date's panel in the special-dates
    tab; all four are optional.
    """

    class Kind(models.TextChoices):
        COMMERCIAL = "commercial", _("Commercial")
        SEASONAL = "seasonal", _("Seasonal")
        NATIONAL = "national", _("National")
        INTERNATIONAL = "international", _("International")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    segment = models.ForeignKey(DateSegment, on_delete=models.CASCADE, null=True, blank=True, related_name="dates")
    name = models.CharField(max_length=120)
    emoji = models.CharField(max_length=8, blank=True, default="")
    month = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    day = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(31)])
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.COMMERCIAL)
    description = models.CharField(max_length=300, blank=True, default="")
    suggestions = models.TextField(blank=True, default="")
    tags = models.CharField(max_length=200, blank=True, default="")
    image = models.ImageField(upload_to="special_dates/", blank=True)
    is_active = models.BooleanField(default=True)
    #: For the task a brand makes of the date ("Create task"): how the arts should be
    #: made and which to produce (``[{"format", "count", "note"}]``, formats of
    #: ``apps.tasks.creative_formats``), as a content template carries them.
    briefing = models.TextField(blank=True, default="", db_default="")
    art_items = models.JSONField(default=list, blank=True, db_default=models.Value([], output_field=models.JSONField()))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "calendar_special_date"
        ordering = ["month", "day", "name"]

    def __str__(self):
        return f"{self.name} ({self.day:02d}/{self.month:02d})"

    @property
    def suggestion_list(self) -> list[str]:
        return [line.strip() for line in self.suggestions.splitlines() if line.strip()]

    @property
    def tag_list(self) -> list[str]:
        return [tag.strip().lstrip("#") for tag in self.tags.split(",") if tag.strip().lstrip("#")]


class WorkspaceDateSegment(models.Model):
    """A segment a brand chose to follow: its dates join the brand's calendar."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="date_segments")
    segment = models.ForeignKey(DateSegment, on_delete=models.CASCADE, related_name="workspace_choices")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "calendar_workspace_date_segment"
        constraints = [
            models.UniqueConstraint(fields=["workspace", "segment"], name="uniq_workspace_date_segment"),
        ]

    def __str__(self):
        return f"{self.workspace_id} → {self.segment_id}"


class FavoriteSpecialDate(models.Model):
    """A special date a brand saved to find again.

    ``key`` names the date whatever its source: ``b:<slug>`` for one built
    into ``holidays.py``, ``d:<uuid>`` for one of the operator's.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="favorite_special_dates"
    )
    key = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "calendar_favorite_special_date"
        constraints = [
            models.UniqueConstraint(fields=["workspace", "key"], name="uniq_workspace_favorite_special_date"),
        ]

    def __str__(self):
        return f"{self.workspace_id} ★ {self.key}"
