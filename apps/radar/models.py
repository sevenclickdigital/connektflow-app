import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.common.encryption import EncryptedJSONField
from apps.common.managers import WorkspaceScopedModel


class OpportunityConnection(WorkspaceScopedModel):
    """A workspace's link to a source of things worth sharing (affiliate offers…).

    Kept apart from ``SocialAccount`` on purpose: a social account is a publish
    target that the composer, publisher, inbox and analytics all iterate, and a
    source like Shopee Affiliates is none of those.
    """

    class Source(models.TextChoices):
        SHOPEE_AFFILIATE = "shopee_affiliate", _("Shopee Affiliates")

    class Status(models.TextChoices):
        CONNECTED = "connected", _("Connected")
        ERROR = "error", _("Error")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=40, choices=Source.choices)
    # Shown on cards so the user knows which affiliate account is linked.
    # Not a secret (the Shopee App ID travels in every request header).
    account_label = models.CharField(max_length=255, blank=True, default="")
    # Source-specific secrets, e.g. {"app_id": ..., "secret": ...}.
    credentials = EncryptedJSONField(default=dict)
    region = models.CharField(max_length=10, default="br")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CONNECTED)
    last_error = models.TextField(blank=True, default="")
    last_checked_at = models.DateTimeField(null=True, blank=True)
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opportunity_connections",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "source"], name="radar_one_connection_per_source"),
        ]

    def __str__(self):
        return f"{self.get_source_display()} ({self.account_label})"


class SavedOffer(WorkspaceScopedModel):
    """An offer a member starred, kept as a snapshot.

    Sources rotate what they list, so a favourite has to survive the offer
    dropping out of the results. ``data`` is the offer as it was when saved
    (``providers.shopee_affiliate.offer_to_dict``); price and commission may
    have changed since.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=40, choices=OpportunityConnection.Source.choices)
    item_id = models.BigIntegerField()
    data = models.JSONField(default=dict)
    saved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="saved_offers",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "source", "item_id"], name="radar_one_saved_offer_per_item"),
        ]

    def __str__(self):
        return f"{self.get_source_display()} {self.item_id}"


class WatchedProfile(WorkspaceScopedModel):
    """An Instagram business or creator account the team keeps an eye on.

    Only the handle is stored. Followers and posts are read live through
    Business Discovery each time the profile is opened, so nothing here goes
    stale or keeps another account's data.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=30)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="watched_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["username"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "username"], name="radar_one_watched_profile_per_handle"),
        ]

    def __str__(self):
        return f"@{self.username}"
