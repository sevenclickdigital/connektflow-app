import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.common.encryption import EncryptedTextField
from apps.common.managers import WorkspaceScopedModel

# How long before the token's expiry the screens start asking to sign in again.
TOKEN_WARNING_DAYS = 7


class AdsConnection(WorkspaceScopedModel):
    """A workspace's link to one Meta ad account, with the Pixel and Page its ads use.

    Kept apart from ``SocialAccount`` on purpose: a social account is a publish
    target that the composer, publisher, inbox and analytics all iterate, and an
    ad account is none of those. Campaigns themselves are not copied here; they
    are read from Meta each time, so what the screen shows is what Meta runs.

    The token is the Meta user's long-lived token (about 60 days). It has no
    refresh: when it lapses the connection turns ``error`` and the person signs
    in again.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Choosing the ad account")
        CONNECTED = "connected", _("Connected")
        ERROR = "error", _("Needs attention")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    access_token = EncryptedTextField()
    token_expires_at = models.DateTimeField(null=True, blank=True)
    # The Facebook user who signed in, shown so the team knows whose access it is.
    meta_user_id = models.CharField(max_length=64, blank=True, default="")
    meta_user_name = models.CharField(max_length=255, blank=True, default="")
    # "act_<number>", as every Marketing API edge takes it.
    ad_account_id = models.CharField(max_length=64, blank=True, default="")
    ad_account_name = models.CharField(max_length=255, blank=True, default="")
    currency = models.CharField(max_length=3, blank=True, default="")
    account_timezone = models.CharField(max_length=64, blank=True, default="")
    pixel_id = models.CharField(max_length=64, blank=True, default="")
    pixel_name = models.CharField(max_length=255, blank=True, default="")
    # The Facebook Page ads are published as.
    page_id = models.CharField(max_length=64, blank=True, default="")
    page_name = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    last_error = models.TextField(blank=True, default="")
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ads_connections",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ads_connection"
        constraints = [
            models.UniqueConstraint(fields=["workspace"], name="ads_one_connection_per_workspace"),
        ]

    def __str__(self):
        return f"Meta Ads ({self.ad_account_name or self.ad_account_id or 'pending'})"

    @property
    def is_ready(self) -> bool:
        """An ad account is chosen and the token was last seen working."""
        return self.status == self.Status.CONNECTED and bool(self.ad_account_id)

    @property
    def token_expired(self) -> bool:
        return bool(self.token_expires_at and self.token_expires_at <= timezone.now())

    @property
    def token_expiring_soon(self) -> bool:
        """Within a week of the date Meta gave, while it still works: time to sign in again."""
        if not self.token_expires_at or self.token_expired:
            return False
        return self.token_expires_at - timezone.now() <= timedelta(days=TOKEN_WARNING_DAYS)
