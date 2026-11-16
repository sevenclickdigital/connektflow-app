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


class MarketHashtag(models.Model):
    """A hashtag in circulation in a line of business, offered to the brands
    that follow that segment (or to every brand, without one).

    Two sources. A ``manual`` one was typed by the operator; it is shown as
    it is and is also a *seed*: the weekly collection reads the most popular
    public posts carrying it on Instagram. A ``meta`` one came out of that
    reading: a hashtag found beside the seeds, with ``posts`` saying in how
    many of the posts read it appeared.
    """

    class Source(models.TextChoices):
        MANUAL = "manual", _("Added by hand")
        META = "meta", _("Found on Instagram")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    segment = models.ForeignKey(
        "calendar.DateSegment", on_delete=models.CASCADE, null=True, blank=True, related_name="market_hashtags"
    )
    tag = models.CharField(max_length=100)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.MANUAL)
    posts = models.PositiveIntegerField(default=0)
    # For a seed: when it was last read on Instagram, and what went wrong if it could not be.
    collected_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "radar_market_hashtag"
        ordering = ["-posts", "tag"]
        constraints = [
            # One row per tag in a segment; the global shelf (no segment) counts as one too.
            models.UniqueConstraint(fields=["segment", "tag"], name="uniq_market_hashtag", nulls_distinct=False),
        ]

    def __str__(self):
        return f"#{self.tag}"


class MarketHashtagCollector(models.Model):
    """The one Instagram account the market hashtags are read through, and
    how the last reading went. A single row (``load``).

    Meta serves hashtag search only to an account connected with Facebook
    Login, on an app with Instagram Public Content Access approved, and
    counts 30 distinct hashtags per account in a rolling seven days.
    """

    account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        db_table = "radar_market_hashtag_collector"

    @classmethod
    def load(cls) -> "MarketHashtagCollector":
        collector, _created = cls.objects.get_or_create(pk=1)
        return collector
