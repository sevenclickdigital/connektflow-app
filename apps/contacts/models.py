import re
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.common.managers import WorkspaceScopedModel

#: Interactions in the last 90 days that make a contact's engagement high or
#: medium; anything below is low. Interactions are the inbox messages the
#: person sent (comments, mentions, tags, DMs, reviews).
ENGAGEMENT_WINDOW_DAYS = 90
ENGAGEMENT_HIGH_FROM = 5
ENGAGEMENT_MEDIUM_FROM = 2


class ChannelKind(models.TextChoices):
    """Where a contact can be reached. Platform keys match ``SocialAccount.platform``."""

    WHATSAPP = "whatsapp", "WhatsApp"
    INSTAGRAM = "instagram", "Instagram"
    FACEBOOK = "facebook", "Facebook"
    MESSENGER = "messenger", "Messenger"
    TIKTOK = "tiktok", "TikTok"
    YOUTUBE = "youtube", "YouTube"
    LINKEDIN = "linkedin", "LinkedIn"
    TELEGRAM = "telegram", "Telegram"
    THREADS = "threads", "Threads"
    X = "x", "X"
    BLUESKY = "bluesky", "Bluesky"
    MASTODON = "mastodon", "Mastodon"
    GOOGLE_BUSINESS = "google_business", "Google Business"
    EMAIL = "email", _("Email")
    PHONE = "phone", _("Phone")


class Contact(WorkspaceScopedModel):
    """A person or company the workspace talks to.

    Created by hand, by a CSV import, or by the inbox the first time someone
    comments or writes (``services.record_inbox_sender``). ``engagement`` is the
    level shown and filtered on: ``engagement_manual`` when the team set one,
    otherwise the level ``interactions_90d`` earns.
    """

    class Kind(models.TextChoices):
        PERSON = "person", _("Individual")
        COMPANY = "company", _("Company")

    class Gender(models.TextChoices):
        FEMALE = "female", _("Female")
        MALE = "male", _("Male")
        OTHER = "other", pgettext_lazy("gender", "Other")
        UNDISCLOSED = "undisclosed", _("Prefer not to say")

    class Status(models.TextChoices):
        LEAD = "lead", _("Lead")
        NEGOTIATING = "negotiating", _("Negotiating")
        ACTIVE = "active", pgettext_lazy("contact status", "Active")
        INACTIVE = "inactive", pgettext_lazy("contact status", "Inactive")

    class Source(models.TextChoices):
        MANUAL = "manual", _("Manual")
        IMPORT = "import", pgettext_lazy("contact source", "Import")
        WHATSAPP = "whatsapp", "WhatsApp"
        INSTAGRAM = "instagram", "Instagram"
        FACEBOOK = "facebook", "Facebook"
        TIKTOK = "tiktok", "TikTok"
        YOUTUBE = "youtube", "YouTube"
        LINKEDIN = "linkedin", "LinkedIn"
        THREADS = "threads", "Threads"
        X = "x", "X"
        BLUESKY = "bluesky", "Bluesky"
        MASTODON = "mastodon", "Mastodon"
        GOOGLE_BUSINESS = "google_business", "Google Business"
        REFERRAL = "referral", _("Referral")
        WEBSITE = "website", _("Website")
        OTHER = "other", pgettext_lazy("contact source", "Other")

    class Engagement(models.TextChoices):
        HIGH = "high", pgettext_lazy("engagement", "High")
        MEDIUM = "medium", pgettext_lazy("engagement", "Medium")
        LOW = "low", pgettext_lazy("engagement", "Low")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PERSON)
    # CPF (person) or CNPJ (company), digits only.
    tax_id = models.CharField(max_length=14, blank=True, default="")
    birth_date = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=12, choices=Gender.choices, blank=True, default="")
    company = models.CharField(max_length=200, blank=True, default="")
    photo = models.ImageField(upload_to="contacts/%Y/%m/", blank=True)
    # The picture the platform gave when the contact came from the inbox.
    avatar_url = models.URLField(max_length=500, blank=True, default="")

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.LEAD, db_index=True)
    funnel = models.CharField(max_length=100, blank=True, default="")
    segment = models.CharField(max_length=100, blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)

    engagement = models.CharField(max_length=6, choices=Engagement.choices, default=Engagement.LOW, db_index=True)
    engagement_manual = models.CharField(max_length=6, choices=Engagement.choices, blank=True, default="")
    interactions_90d = models.PositiveIntegerField(default=0)
    last_contact_at = models.DateTimeField(null=True, blank=True)

    address = models.CharField(max_length=255, blank=True, default="")
    city = models.CharField(max_length=120, blank=True, default="")
    state = models.CharField(max_length=2, blank=True, default="")
    postal_code = models.CharField(max_length=9, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_contacts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "contacts_contact"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["workspace", "name"], name="contact_ws_name"),
            models.Index(fields=["workspace", "-last_contact_at"], name="contact_ws_last_contact"),
        ]

    def __str__(self):
        return self.name

    @staticmethod
    def level_for(interactions: int) -> str:
        """The engagement level ``interactions`` in the window earn."""
        if interactions >= ENGAGEMENT_HIGH_FROM:
            return Contact.Engagement.HIGH
        if interactions >= ENGAGEMENT_MEDIUM_FROM:
            return Contact.Engagement.MEDIUM
        return Contact.Engagement.LOW

    def refresh_engagement(self) -> None:
        """Set ``engagement`` from the manual level, or from the interactions."""
        self.engagement = self.engagement_manual or self.level_for(self.interactions_90d)

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.split() if p[:1].isalnum()]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    @property
    def picture_url(self) -> str:
        if self.photo:
            return self.photo.url
        return self.avatar_url if self.avatar_url.startswith("https://") else ""


class ContactChannel(models.Model):
    """One way to reach a contact: a phone on WhatsApp, an Instagram @, an email.

    ``external_id`` is the person's platform-scoped ID when the channel came
    from the inbox; it is what a later message from the same person matches on,
    since a handle can change and the webhook only carries the ID.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="contact_channels")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="channels")
    kind = models.CharField(max_length=20, choices=ChannelKind.choices)
    value = models.CharField(max_length=255)
    external_id = models.CharField(max_length=255, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contacts_channel"
        ordering = ["position", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "kind", "external_id"],
                condition=~Q(external_id=""),
                name="contact_channel_one_person_per_platform_id",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace", "kind", "value"], name="contact_channel_ws_kind_value"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.value}"

    @property
    def display_value(self) -> str:
        """The value as people read it: a Brazilian number as +55 48 99123-4567."""
        if self.kind in (ChannelKind.WHATSAPP, ChannelKind.PHONE):
            match = re.fullmatch(r"\+55(\d{2})(\d{4,5})(\d{4})", self.value)
            if match:
                return f"+55 {match[1]} {match[2]}-{match[3]}"
        return self.value
