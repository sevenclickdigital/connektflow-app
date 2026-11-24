"""Client Portal models (F-1.4).

Models:
    MagicLinkToken - Time-limited token for passwordless client access.
"""

import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

# HKDF ``info`` for the magic-link pepper. Changing it invalidates every link
# already sent.
MAGIC_LINK_HMAC_INFO = b"brightbean-magic-link-hmac"


def _generate_magic_token():
    return secrets.token_urlsafe(48)


def hash_magic_token(raw_token: str) -> str:
    """The at-rest form of a magic-link token (peppered HMAC, hex)."""
    from apps.api_keys.services import peppered_hmac

    return peppered_hmac(raw_token, info=MAGIC_LINK_HMAC_INFO)


def _default_expiry():
    return timezone.now() + timedelta(days=30)


class MagicLinkToken(models.Model):
    """A magic link token for passwordless client portal access.

    Only ``token_hash`` is stored; the raw token exists in the e-mailed URL
    alone (``services.issue_magic_link_token``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="magic_link_tokens",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="magic_link_tokens",
    )
    # Plaintext column of the previous scheme. Never written or read; it stays
    # until the release after this one so code still running against it during
    # a rolling deploy does not break, and is then dropped.
    token = models.CharField(max_length=128, unique=True, db_index=True, null=True, blank=True, editable=False)
    token_hash = models.CharField(max_length=64, null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=_default_expiry)
    last_used_at = models.DateTimeField(null=True, blank=True)
    is_consumed = models.BooleanField(default=False)

    class Meta:
        db_table = "client_portal_magic_link_token"
        indexes = [
            models.Index(fields=["user", "workspace"], name="idx_magic_user_ws"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["token_hash"], name="uniq_magic_token_hash"),
        ]

    def __str__(self):
        return f"MagicLink for {self.user} → {self.workspace}"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_valid(self):
        return not self.is_expired
