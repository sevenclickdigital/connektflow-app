"""Sales funnels over inbox conversations.

A workspace has one or more funnels, each an ordered list of stages. A
conversation of the inbox (``apps.inbox.services.thread_key``: one person,
account and kind, and for comments one post) sits in at most one funnel as an
``Opportunity``. A conversation with no opportunity counts as the first stage
of the workspace's default funnel, so every new conversation starts there
without a row being written for it.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.common.managers import WorkspaceScopedModel


class Funnel(WorkspaceScopedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)
    position = models.PositiveSmallIntegerField(default=0)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "funnels_funnel"
        ordering = ["position", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace"], condition=Q(is_default=True), name="funnel_one_default_per_workspace"
            ),
        ]

    def __str__(self):
        return self.name


class FunnelStage(models.Model):
    class Kind(models.TextChoices):
        OPEN = "open", pgettext_lazy("funnel stage kind", "In progress")
        WON = "won", pgettext_lazy("funnel stage kind", "Won")
        LOST = "lost", pgettext_lazy("funnel stage kind", "Lost")

    # Palette keys, drawn by .funnel-stage-<color> in styles.css.
    class Color(models.TextChoices):
        VIOLET = "violet", _("Violet")
        BLUE = "blue", _("Blue")
        AMBER = "amber", _("Amber")
        PURPLE = "purple", _("Purple")
        GREEN = "green", _("Green")
        RED = "red", _("Red")
        TEAL = "teal", _("Teal")
        GRAY = "gray", _("Gray")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    funnel = models.ForeignKey(Funnel, on_delete=models.CASCADE, related_name="stages")
    name = models.CharField(max_length=60)
    color = models.CharField(max_length=10, choices=Color.choices, default=Color.VIOLET)
    kind = models.CharField(max_length=5, choices=Kind.choices, default=Kind.OPEN)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "funnels_stage"
        ordering = ["position", "name"]

    def __str__(self):
        return self.name


class Opportunity(WorkspaceScopedModel):
    """A conversation's place in a funnel, with what the team knows of the deal.

    Keyed on the conversation like the inbox lists it (``THREAD_FIELDS``), not on
    one message: every message of the conversation shares it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    funnel = models.ForeignKey(Funnel, on_delete=models.CASCADE, related_name="opportunities")
    stage = models.ForeignKey(FunnelStage, on_delete=models.PROTECT, related_name="opportunities")
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.CASCADE, related_name="opportunities"
    )
    message_type = models.CharField(max_length=20)
    thread_sender = models.CharField(max_length=255)
    thread_post = models.CharField(max_length=255, blank=True, default="")
    # Who the conversation is with, as the inbox showed them when it was written.
    name = models.CharField(max_length=255, blank=True, default="")
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    interest = models.CharField(max_length=100, blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True, default="")
    stage_changed_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "funnels_opportunity"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "social_account", "message_type", "thread_sender", "thread_post"],
                name="opportunity_one_per_conversation",
            ),
        ]
        indexes = [models.Index(fields=["funnel", "stage"], name="opportunity_funnel_stage")]

    def __str__(self):
        return f"{self.thread_sender} @ {self.stage}"


class OpportunityActivity(WorkspaceScopedModel):
    """What happened to an opportunity, for the board's recent activity."""

    class Kind(models.TextChoices):
        MOVED = "moved", _("Moved")
        UPDATED = "updated", _("Updated")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="activities")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    stage = models.ForeignKey(FunnelStage, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "funnels_activity"
        ordering = ["-created_at"]
