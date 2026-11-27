import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import models, router, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.common.encryption import EncryptedTextField
from apps.common.managers import WorkspaceScopedManager
from apps.credentials.models import PlatformCredential

# Platforms connected only to talk to customers: nothing is published, scheduled
# or measured on them, so the composer, calendar, analytics and automations
# leave them out (``SocialAccount.objects.publishable()``), while the inbox and
# the Social Accounts page show them like any other account.
MESSAGING_ONLY_PLATFORMS = frozenset({"whatsapp"})


def default_format_short(formats):
    """The ordinary post's short name next to an account's other formats:
    "Feed" beside Stories, "Video" beside Shorts, "Post" elsewhere."""
    if "story" in formats:
        return _("Feed")
    if "short" in formats:
        return _("Video")
    return _("Post")


class SocialAccountQuerySet(models.QuerySet):
    def publishable(self):
        """Accounts that posts can be published to."""
        return self.exclude(platform__in=MESSAGING_ONLY_PLATFORMS)

    def in_display_order(self):
        """The order the brand set on the Social Accounts page; accounts it
        never placed (all of them until it reorders, and any connected
        after) follow, by platform and name."""
        return self.order_by(models.F("position").asc(nulls_last=True), "platform", "account_name", "id")


class YouTubeRevocation(models.Model):
    """Encrypted, short-lived grant retry, independent of a deleted account."""

    token = EncryptedTextField()
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    next_attempt_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveIntegerField(default=0)


class SocialAccount(models.Model):
    _skip_analytics_backfill: bool

    class AnalyticsStatus(models.TextChoices):
        UNKNOWN = "unknown", _("Not checked")
        CHECKING = "checking", _("Checking")
        READY = "ready", _("Ready")
        RECONNECT = "reconnect_required", _("Reconnect required")
        ATTENTION = "attention_required", _("Operator attention required")
        TEMPORARY = "temporary_error", _("Temporary failure")

    class ConnectionStatus(models.TextChoices):
        CONNECTED = "connected", _("Connected")
        TOKEN_EXPIRING = "token_expiring", _("Token Expiring")
        DISCONNECTED = "disconnected", _("Disconnected")
        ERROR = "error", _("Error")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="social_accounts",
    )
    platform = models.CharField(
        max_length=30,
        choices=PlatformCredential.Platform.choices,
    )
    account_platform_id = models.CharField(
        max_length=255,
        help_text="The account's native ID on the platform.",
    )
    account_name = models.CharField(max_length=255)
    account_handle = models.CharField(max_length=255, blank=True, default="")
    avatar_url = models.URLField(max_length=2000, blank=True, default="")
    follower_count = models.IntegerField(default=0)

    # Encrypted OAuth tokens
    oauth_access_token = EncryptedTextField(blank=True, default="")
    oauth_refresh_token = EncryptedTextField(blank=True, default="")
    token_expires_at = models.DateTimeField(blank=True, null=True)
    # Facebook-login Instagram insights need the user's credential. The
    # primary token remains the Page credential used by publishing and inbox.
    oauth_facebook_user_access_token = EncryptedTextField(blank=True, default="")
    facebook_user_token_expires_at = models.DateTimeField(blank=True, null=True)
    analytics_status = models.CharField(max_length=25, choices=AnalyticsStatus.choices, default=AnalyticsStatus.UNKNOWN)
    analytics_error_reason = models.CharField(max_length=40, blank=True, default="")
    analytics_checked_at = models.DateTimeField(blank=True, null=True)
    analytics_credentials_version = models.PositiveIntegerField(default=0)

    # Instance URL for Mastodon and Bluesky PDS
    instance_url = models.URLField(max_length=500, blank=True, default="")

    # Object we subscribed for webhook delivery, when it isn't this account
    # itself. For Facebook Login Instagram this holds the linked Page ID used
    # for DM polling, sending and message subscriptions. Comment subscriptions
    # still address the Instagram account itself.
    webhook_target_id = models.CharField(max_length=255, blank=True, default="")
    messaging_access = models.BooleanField(null=True, blank=True, default=None)

    # Whether the platform is currently pushing this account's activity to us.
    # Null means "not applicable" (the platform has no webhooks) or "not tried
    # yet". False means the inbox will miss comments and mentions, which is
    # invisible without saying so — publishing and analytics still work, so the
    # connection itself stays healthy and `last_error` (owned by the periodic
    # health check) must not be borrowed for it.
    webhooks_active = models.BooleanField(null=True, blank=True, default=None)
    webhook_error = models.CharField(max_length=500, blank=True, default="")
    # Set when the subscription failed for a reason a retry cannot fix: the
    # grant itself is missing what it needs. Splits the card's CTA between
    # "Try again" (re-run the call) and "Reconnect" (get a new grant), so the
    # warning never asks for something the UI doesn't offer.
    webhook_needs_reconnect = models.BooleanField(default=False)
    # The provider's own words about the last failure. `webhook_error` is
    # rewritten for a human and is all the card shows; operators running
    # `diagnose_facebook` need the error code and fbtrace_id this keeps.
    webhook_error_detail = models.TextField(blank=True, default="")
    # Consecutive failed attempts, so the periodic health check can stop
    # re-trying a subscription that will never succeed. Reset by a success, by
    # a reconnect, and by the user pressing "Try again".
    webhook_retry_count = models.PositiveSmallIntegerField(default=0)

    # Scopes we asked for that the grant came back without. Meta drops
    # unapproved or declined permissions silently rather than failing the
    # grant, so without this the account looks healthy and only breaks later at
    # publish or insights time with an opaque platform error. Empty means
    # "everything we asked for was granted", or that the platform gives us no
    # way to ask.
    missing_scopes = models.JSONField(default=list, blank=True)

    # Connection health
    connection_status = models.CharField(
        max_length=20,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.CONNECTED,
    )
    last_health_check_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True, default="")

    # The person on the platform who granted this connection, when that is not
    # the account itself: with Facebook Login the account is a Page or an
    # Instagram profile and the grant belongs to a Facebook user. It is the id
    # Meta sends when that person asks for their data to be deleted. Empty when
    # the platform did not tell us.
    granted_by_platform_user_id = models.CharField(max_length=255, blank=True, default="", db_default="")

    connected_at = models.DateTimeField(auto_now_add=True)
    # Place among the brand's accounts, set by dragging the cards on the
    # Social Accounts page (``services.reorder_accounts``); see
    # ``SocialAccountQuerySet.in_display_order``.
    position = models.PositiveIntegerField(blank=True, null=True)
    youtube_authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="authorized_youtube_accounts",
    )
    youtube_authorization_checked_at = models.DateTimeField(null=True, blank=True)
    youtube_profile_refreshed_at = models.DateTimeField(null=True, blank=True)
    youtube_verification_started_at = models.DateTimeField(default=timezone.now)
    youtube_data_generation = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Per-account override of the Agent-API platform daily-post quota
    # (see apps/api/limits.py::PLATFORM_DAILY_POST_LIMIT). Null = use the
    # platform default. Useful when one specific integration is on a higher
    # upstream tier (e.g. an X account on Pro vs the default Basic cap).
    daily_post_limit_override = models.PositiveIntegerField(blank=True, null=True)

    # Set by the analytics sync layer when the platform rejects an analytics
    # call as insufficient-scope. Surfaces a "Reconnect for analytics" CTA in
    # place of the metric region. Cleared on successful reconnect.
    analytics_needs_reconnect = models.BooleanField(default=False)

    # When the inbox last polled this account, and when it last walked the
    # account's full history. Both belong here rather than being derived from
    # InboxMessage.received_at, which answers a different question: an account
    # that was polled and had nothing new to say leaves no message behind, so
    # the derived value would say "never polled" and re-poll it every cycle.
    #
    # The poll stamp is what lets one 5-minute cycle serve platforms with very
    # different budgets — see settings.INBOX_PLATFORM_MIN_POLL_SECONDS. The
    # sweep stamp paces the deep walk that catches a reply on a thread too old
    # for the routine poll's lookback (providers.youtube.get_messages).
    # Where this account's inbox begins. A new connection starts empty: the
    # polls drop anything said before this moment, so the workspace is not
    # handed years of old comments and conversations it never asked for. Null
    # means no floor: an account connected before the floor existed, or one
    # whose history was asked for ("Import history" on its card).
    inbox_history_cutoff = models.DateTimeField(blank=True, null=True)
    # The last "Import history" / "Remove old history" asked of this account,
    # so its card can say it is running, how it ended, or why it failed:
    # {"action": "import"|"clear", "state": "running"|"done"|"failed",
    #  "started_at": iso, "count": int, "error": str}. Null when never asked.
    inbox_history_job = models.JSONField(blank=True, null=True)
    inbox_last_polled_at = models.DateTimeField(blank=True, null=True)
    inbox_last_deep_sweep_at = models.DateTimeField(blank=True, null=True)
    inbox_initial_backfill_cursor = models.TextField(blank=True, default="")
    inbox_initial_backfill_started_at = models.DateTimeField(blank=True, null=True)
    inbox_deep_sweep_cursor = models.TextField(blank=True, default="")
    inbox_deep_sweep_started_at = models.DateTimeField(blank=True, null=True)
    # Resume bounded Facebook Login Instagram DM walks across worker restarts.
    # Contains only object IDs/cursors, never OAuth tokens or message bodies.
    instagram_dm_poll_state = models.JSONField(default=dict, blank=True)

    objects = WorkspaceScopedManager.from_queryset(SocialAccountQuerySet)()

    class Meta:
        db_table = "social_accounts_social_account"
        unique_together = [("workspace", "platform", "account_platform_id")]

    def __str__(self):
        return f"{self.account_name} ({self.get_platform_display()})"

    def save(self, *args, **kwargs):
        """Invalidate Instagram checks atomically when either credential changes."""
        credential_fields = {"oauth_access_token", "oauth_facebook_user_access_token", "facebook_user_token_expires_at"}
        fields = kwargs.get("update_fields")
        self._analytics_credentials_changed = False
        if self.platform != "instagram" or (fields is not None and not credential_fields.intersection(fields)):
            return super().save(*args, **kwargs)
        using = kwargs.get("using") or self._state.db or "default"
        with transaction.atomic(using=using):
            previous = type(self).objects.using(using).select_for_update().filter(pk=self.pk).first()
            changed = previous is not None and any(
                getattr(previous, field) != getattr(self, field)
                for field in credential_fields
                if fields is None or field in fields
            )
            # OAuth may return the same token after a consent change. A fresh
            # connection still gets a new verification generation.
            rechecking = (
                fields is not None
                and {"analytics_status", "oauth_facebook_user_access_token"}.issubset(fields)
                and self.analytics_status == self.AnalyticsStatus.CHECKING
            )
            if changed or rechecking or (previous is None and self.oauth_access_token):
                self._analytics_credentials_changed = True
                self.__dict__.pop("_instagram_analytics_grant", None)
                self.__dict__.pop("_instagram_analytics_status_recorded", None)
                self.analytics_credentials_version = (previous.analytics_credentials_version if previous else 0) + 1
                self.analytics_status = self.AnalyticsStatus.CHECKING
                self.analytics_error_reason = ""
                self.analytics_checked_at = None
                self.analytics_needs_reconnect = False
                if fields is not None:
                    kwargs["update_fields"] = set(fields) | {
                        "analytics_credentials_version",
                        "analytics_status",
                        "analytics_error_reason",
                        "analytics_checked_at",
                        "analytics_needs_reconnect",
                    }
            return super().save(*args, **kwargs)

    @property
    def instagram_dm_permission_request_disabled(self):
        return (
            self.platform == "instagram"
            and "instagram_manage_messages" in self.missing_scopes
            and not settings.PLATFORM_INSTAGRAM_FACEBOOK_DM_SCOPES_ENABLED
        )

    @property
    def connection_missing_scopes(self):
        # Instagram analytics has its own grant-aware status. A Page token's
        # scope list cannot establish whether the person declined insights.
        return [
            scope
            for scope in self.missing_scopes
            if self.platform != "instagram" or scope != "instagram_manage_insights"
            if scope != "instagram_manage_messages" or not self.instagram_dm_permission_request_disabled
        ]

    @property
    def webhook_warning(self):
        """The subscription warning and action applicable to current OAuth scopes."""
        from .error_messages import WebhookFailure, omit_disabled_instagram_dm_failure

        if self.webhooks_active is not False:
            return None
        failure = WebhookFailure(self.webhook_error, self.webhook_needs_reconnect)
        if self.instagram_dm_permission_request_disabled and "pages_manage_metadata" not in self.missing_scopes:
            return omit_disabled_instagram_dm_failure(failure)
        return failure

    @property
    def instagram_analytics_message(self):
        if self.platform != "instagram":
            return ""
        from apps.analytics.instagram_access import status_message

        return status_message(self)

    @property
    def display_label(self) -> str:
        """Human name for pickers and filters: the account name, else the handle.

        Mirrors the ``account_name|default:account_handle`` the channel filters
        used inline, so a component that can only read one attribute (the
        ``ui_select`` tag's ``label_field``) still renders the same text.
        """
        return self.account_name or self.account_handle

    def token_expires_within(self, window) -> bool:
        """Whether the recorded expiry falls inside ``window`` from now.

        An unknown expiry (``token_expires_at is None``) answers False: we
        cannot judge it, and the platforms where "unknown" should mean "refresh
        anyway" are named explicitly in ``tasks.EXPIRY_BOOTSTRAP_PLATFORMS``.

        The window is a caller's decision because it means different things at
        different cadences. A user-triggered publish can afford the generous
        7-day view. An hourly background loop cannot: a Google access token
        lives one hour, so *any* window wider than that is permanently true and
        would spend a refresh call every hour on every account for nothing.
        """
        if not self.token_expires_at:
            return False
        from django.utils import timezone

        return self.token_expires_at < timezone.now() + window

    @property
    def is_token_expiring_soon(self) -> bool:
        """Token expires within 7 days."""
        from datetime import timedelta

        return self.token_expires_within(timedelta(days=7))

    # A job still "running" after this long lost its worker; the card stops
    # waiting for it and offers the action again.
    INBOX_HISTORY_JOB_TIMEOUT = timedelta(minutes=30)

    @property
    def inbox_history_job_state(self) -> str:
        """``running``, ``done``, ``failed`` or "" for the account's last history job."""
        job = self.inbox_history_job or {}
        state = job.get("state", "")
        if state != "running":
            return state
        started = parse_datetime(job.get("started_at") or "")
        if started is None or started < timezone.now() - self.INBOX_HISTORY_JOB_TIMEOUT:
            return ""
        return "running"

    def set_inbox_history_job(self, action: str, state: str, **details) -> None:
        """Record where a history job stands, without touching the rest of the row."""
        job = {"action": action, "state": state, **details}
        if state == "running":
            job["started_at"] = timezone.now().isoformat()
        type(self).objects.filter(pk=self.pk).update(inbox_history_job=job)
        self.inbox_history_job = job

    @property
    def _reads_inbox_history(self) -> bool:
        from providers import polls_inbox

        return polls_inbox(self.platform) and self.inbox_history_job_state != "running"

    @property
    def can_import_inbox_history(self) -> bool:
        """Whether "Import history" has anything to do: the inbox still has a floor."""
        return self.inbox_history_cutoff is not None and self._reads_inbox_history

    @property
    def can_clear_inbox_history(self) -> bool:
        """Whether "Remove old history" has anything to do: the inbox has no floor."""
        return self.inbox_history_cutoff is None and self._reads_inbox_history

    @property
    def connect_flow(self) -> str:
        """How this platform's connection starts (``catalog.connect_flow``)."""
        from .catalog import connect_flow

        return connect_flow(self.platform)

    @property
    def needs_reconnect(self) -> bool:
        return self.connection_status in (
            self.ConnectionStatus.DISCONNECTED,
            self.ConnectionStatus.ERROR,
        )

    def can_delete_post(self, post_format: str) -> bool:
        """Whether a published post of ``post_format`` can be deleted from the
        network through this connection: the platform's API has to allow it and,
        where deleting is a permission of its own, this deployment has to ask
        for it and the grant has to carry it."""
        import providers

        if not providers.can_delete_post(self.platform, post_format):
            return False
        scope = providers.delete_post_scope(self.platform)
        if not scope:
            return True
        return settings.PLATFORM_INSTAGRAM_FACEBOOK_DELETE_SCOPE_ENABLED and scope not in (self.missing_scopes or [])

    # The permission each Meta chat channel needs for DMs to reach the inbox.
    _MESSAGING_SCOPES = {
        "facebook": "pages_messaging",
        "instagram": "instagram_manage_messages",
        "instagram_login": "instagram_business_manage_messages",
    }

    @property
    def messaging_state(self) -> str:
        """Whether this account's private messages reach the inbox.

        ``active`` or ``unavailable`` for the chat channels (Messenger,
        Instagram Direct, WhatsApp), ``""`` for platforms without one. Mirrors
        what ``inbox.reply_policy`` refuses on, so the card and the inbox agree.
        """
        if self.platform == "whatsapp":
            return "unavailable" if self.needs_reconnect or self.webhooks_active is False else "active"
        scope = self._MESSAGING_SCOPES.get(self.platform)
        if not scope:
            return ""
        if (
            self.needs_reconnect
            or scope in (self.missing_scopes or [])
            or self.messaging_access is False
            or self.webhooks_active is False
            or (
                self.platform == "instagram"
                and (self.instagram_dm_permission_request_disabled or not self.webhook_target_id)
            )
        ):
            return "unavailable"
        return "active"

    @property
    def health(self) -> str:
        """``error`` when the connection is broken, ``warning`` when it works
        with something missing (permissions, real-time updates, an expiring
        token), else ``ok``. Drives the status pill on the account card; the
        details behind a warning are the properties above."""
        if self.needs_reconnect:
            return "error"
        if (
            self.connection_status == self.ConnectionStatus.TOKEN_EXPIRING
            or self.connection_missing_scopes
            or self.webhook_warning
            or self.instagram_dm_permission_request_disabled
            or self.messaging_access is False
        ):
            return "warning"
        return "ok"

    def refresh_oauth_token(self, provider, *, enqueue_backfill: bool = True) -> str:
        """Refresh this account's OAuth access token via *provider* and persist it.

        Returns the new access token. Propagates whatever the provider's
        ``refresh_token`` raises so callers decide between degrading (publish
        engine keeps the old token) and aborting (composer endpoints 502).

        ``enqueue_backfill=False`` is for routine token rotations (publishing,
        analytics, inbox polls, and composer lookups). A rotation is not a new
        connection and must not queue a full historical analytics backfill.

        Serialize refreshes with a row lock on PostgreSQL or an account lock
        within the SQLite worker. A caller that waited for another refresh uses
        its persisted credentials rather than resubmitting the refresh token.
        """
        from datetime import timedelta

        from django.utils import timezone

        from .token_refresh import token_refresh_lock, token_refresh_transaction

        using = router.db_for_write(type(self), instance=self)
        accounts = type(self).objects.using(using)
        token_fields = ("oauth_access_token", "oauth_refresh_token", "token_expires_at")
        try:
            with token_refresh_lock(using, self.pk):
                with token_refresh_transaction(using):
                    current = accounts.select_for_update().get(pk=self.pk)
                    if self.platform == "youtube" and current.youtube_data_generation != self.youtube_data_generation:
                        from providers.exceptions import TokenExpiredError

                        raise TokenExpiredError("YouTube connection changed during token refresh", platform="youtube")
                if all(getattr(current, field) == getattr(self, field) for field in token_fields):
                    new_tokens = provider.refresh_token(current.oauth_refresh_token)
                    # SQLite has no row lock during HTTP. Discard a result if
                    # another process replaced the stored credentials meanwhile.
                    with token_refresh_transaction(using):
                        current = accounts.select_for_update().get(pk=self.pk)
                        if (
                            self.platform == "youtube"
                            and current.youtube_data_generation != self.youtube_data_generation
                        ):
                            from providers.exceptions import TokenExpiredError

                            raise TokenExpiredError(
                                "YouTube connection changed during token refresh", platform="youtube"
                            )
                        if all(getattr(current, field) == getattr(self, field) for field in token_fields):
                            current.oauth_access_token = new_tokens.access_token
                            if new_tokens.refresh_token:
                                current.oauth_refresh_token = new_tokens.refresh_token
                            if new_tokens.expires_in:
                                current.token_expires_at = timezone.now() + timedelta(seconds=new_tokens.expires_in)
                            current.connection_status = self.ConnectionStatus.CONNECTED
                            current._skip_analytics_backfill = not enqueue_backfill
                            current.save(using=using, update_fields=[*token_fields, "connection_status", "updated_at"])
                state_fields = [*token_fields, "connection_status"]
                if self.platform == "instagram":
                    # save() invalidates Instagram analytics on a rotation.
                    # Keep the caller's generation and cached verdicts in sync
                    # even though the locked instance performed that save.
                    if current.analytics_credentials_version != self.analytics_credentials_version:
                        self.__dict__.pop("_instagram_analytics_grant", None)
                        self.__dict__.pop("_instagram_analytics_status_recorded", None)
                    state_fields.extend(
                        [
                            "oauth_facebook_user_access_token",
                            "facebook_user_token_expires_at",
                            "analytics_credentials_version",
                            "analytics_status",
                            "analytics_error_reason",
                            "analytics_checked_at",
                            "analytics_needs_reconnect",
                        ]
                    )
                for field in state_fields:
                    setattr(self, field, getattr(current, field))
        except Exception as exc:
            if self.platform == "youtube":
                from .youtube_compliance import permanent_grant_rejection, purge_connection

                # Purge after the failed refresh transaction has rolled back.
                if permanent_grant_rejection(exc):
                    purge_connection(self, check_generation=True)
            raise
        return self.oauth_access_token

    # Platform character limits
    PLATFORM_CHAR_LIMITS = {
        "facebook": 63206,
        "instagram": 2200,
        "instagram_login": 2200,
        "linkedin_personal": 3000,
        "linkedin_company": 3000,
        "tiktok": 2200,
        "youtube": 5000,
        "pinterest": 500,
        "threads": 500,
        "bluesky": 300,
        "google_business": 1500,
        "mastodon": 500,
        "devto": 25000,
        "x": 280,
    }

    @property
    def char_limit(self) -> int:
        return self.PLATFORM_CHAR_LIMITS.get(self.platform, 2200)

    @property
    def escaped_chars(self) -> str:
        """Characters this platform escapes, each costing two against the limit."""
        from providers import CAPTION_ESCAPED_CHARS

        return CAPTION_ESCAPED_CHARS.get(self.platform, "")

    def caption_wire_length(self, text: str) -> int:
        """Caption length as this platform counts it, after any escaping.

        LinkedIn escapes reserved characters in the commentary it publishes, so
        the typed length is not the length that counts against ``char_limit``.
        """
        from providers import caption_wire_length

        return caption_wire_length(self.platform, text)

    # Platform-specific field configuration (which platforms need extra fields)
    PLATFORM_FIELD_CONFIG: dict[str, dict[str, Any]] = {
        "youtube": {
            "needs_title": True,
            "title_max_length": 100,
            "title_label": _("Video Title"),
            "caption_label": _("Description"),
            "advanced_fields": ["made_for_kids", "privacy_status", "tags", "thumbnail"],
            "formats": ["default", "short"],
        },
        "pinterest": {
            "needs_title": True,
            "title_max_length": 100,
            "title_label": _("Pin Title"),
            "caption_label": _("Description"),
            "supports_first_comment": False,
            "advanced_fields": ["allow_comments", "show_similar_products", "alt_text", "cover_image"],
        },
        "tiktok": {
            "supports_first_comment": False,
        },
        "bluesky": {
            "supports_first_comment": False,
        },
        "facebook": {
            "formats": ["default", "story", "reel"],
        },
        "instagram": {
            "formats": ["default", "story", "reel"],
        },
        "instagram_login": {
            "formats": ["default", "story", "reel"],
        },
        "google_business": {
            "supports_first_comment": False,
            "formats": ["default", "profile_photo"],
        },
        "x": {
            "supports_first_comment": False,
        },
        "devto": {
            "needs_title": True,
            "title_max_length": 128,
            "title_label": _("Article Title"),
            "caption_label": _("Body (Markdown)"),
            "supports_first_comment": False,
        },
    }

    PLATFORM_FIELD_DEFAULTS = {
        "needs_title": False,
        "title_max_length": 0,
        "title_label": _("Title"),
        "caption_label": _("Caption"),
        "supports_first_comment": True,
        "advanced_fields": [],
        # PlatformPost formats one post can go out as on this account. A post
        # may take several at once; each becomes its own PlatformPost.
        "formats": ["default"],
    }

    @property
    def field_config(self) -> dict:
        """Return field configuration for this platform."""
        return {**self.PLATFORM_FIELD_DEFAULTS, **self.PLATFORM_FIELD_CONFIG.get(self.platform, {})}

    @property
    def formats(self) -> list[str]:
        return self.field_config["formats"]

    def format_options(self) -> list[dict]:
        """``[{"format", "label", "short"}]`` for the composer, in display order.

        ``label`` names what the user gets ("Instagram Stories"); the ordinary
        post is just the platform's name. ``short`` is the format alone, for a
        toggle or chip next to the account, the ordinary post named by
        ``default_format_short``.
        """
        platform = self.get_platform_display()
        labels = {
            "default": platform,
            "story": _("%(platform)s Stories") % {"platform": platform},
            "reel": _("%(platform)s Reels") % {"platform": platform},
            "short": _("%(platform)s Shorts") % {"platform": platform},
            "profile_photo": _("%(platform)s photo") % {"platform": platform},
        }
        shorts = {
            "default": default_format_short(self.formats),
            "story": pgettext_lazy("publishing format", "Story"),
            "reel": _("Reels"),
            "short": _("Shorts"),
            "profile_photo": _("Photo"),
        }
        return [{"format": fmt, "label": str(labels[fmt]), "short": str(shorts[fmt])} for fmt in self.formats]

    @property
    def keeps_platform_grant_on_disconnect(self) -> bool:
        """True when disconnecting here cannot revoke the platform's grant.

        The Facebook-Page flows share one grant across every Page and Instagram
        account that person connected, so the only endpoint that would revoke
        it takes all of them down at once — see
        ``FacebookProvider.revoke_token``. Instagram Login is excluded: its
        token belongs to the one account, so disconnect does revoke it.
        """
        return self.platform in {
            PlatformCredential.Platform.FACEBOOK,
            PlatformCredential.Platform.INSTAGRAM,
        }

    @property
    def revokes_platform_grant_on_disconnect(self) -> bool:
        """True when disconnecting also revokes our access on the platform.

        The disconnect confirmation promises exactly this, so it lists only the
        providers whose ``revoke_token`` calls a revocation endpoint with the
        token disconnect hands it. Left out, besides the Facebook-Page flows:
        Pinterest, Threads and DEV.to have no revocation call. Bluesky counts:
        deleting the session ends the only access we hold, since the app
        password it was created with is never stored.
        """
        return self.platform in {
            PlatformCredential.Platform.YOUTUBE,
            PlatformCredential.Platform.GOOGLE_BUSINESS,
            PlatformCredential.Platform.TIKTOK,
            PlatformCredential.Platform.LINKEDIN_PERSONAL,
            PlatformCredential.Platform.LINKEDIN_COMPANY,
            PlatformCredential.Platform.MASTODON,
            PlatformCredential.Platform.INSTAGRAM_LOGIN,
            PlatformCredential.Platform.X,
            PlatformCredential.Platform.BLUESKY,
        }

    @property
    def revocation_token(self) -> str:
        """The token disconnect should hand the provider's ``revoke_token``.

        Google refuses to revoke an expired access token, and ours lives an
        hour, so by the time someone disconnects it is usually dead and the
        grant would survive. The refresh token revokes the same grant and
        outlives it. Bluesky deletes a session only for its refresh JWT, and
        answers the access JWT with "Invalid token type".
        """
        if (
            self.platform
            in {
                PlatformCredential.Platform.YOUTUBE,
                PlatformCredential.Platform.GOOGLE_BUSINESS,
                PlatformCredential.Platform.BLUESKY,
            }
            and self.oauth_refresh_token
        ):
            return self.oauth_refresh_token
        return self.oauth_access_token

    def supports_first_comment(self) -> bool:
        """Whether this account can have a first comment posted by the worker.

        Most platforms answer purely from PLATFORM_FIELD_CONFIG. LinkedIn Personal
        is the exception: in OIDC mode the socialActions.CREATE endpoint returns
        403 ACCESS_DENIED because that endpoint is gated on Community Management
        API approval. Resolve credentials and check ``_oauth_mode`` for it.
        """
        if not self.field_config.get("supports_first_comment", True):
            return False
        if self.platform == "linkedin_personal":
            from apps.publisher.engine import _resolve_publish_credentials

            creds = _resolve_publish_credentials(self)
            if creds.get("_oauth_mode", "oidc") == "oidc":
                return False
        return True

    @property
    def platform_icon(self) -> str:
        """Short icon label for platform badges."""
        icons = {
            "facebook": "f",
            "instagram": "ig",
            "instagram_login": "ig",
            "linkedin_personal": "in",
            "linkedin_company": "in",
            "tiktok": "tk",
            "youtube": "yt",
            "pinterest": "pi",
            "threads": "th",
            "bluesky": "bs",
            "google_business": "gb",
            "mastodon": "ma",
            "devto": "dv",
            "x": "x",
        }
        return icons.get(self.platform, self.platform[:2])


class DataDeletionRequest(models.Model):
    """A platform's "delete this person's data" callback, as it was answered.

    The platform shows the person a confirmation code and a link; this row is
    what that link reads. It records that the request came and what was
    removed, and deliberately not who asked: keeping the platform user id
    would be keeping the data we were told to delete.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    confirmation_code = models.CharField(max_length=32, unique=True)
    platform = models.CharField(max_length=30, choices=PlatformCredential.Platform.choices)
    accounts_removed = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "social_accounts_data_deletion_request"

    def __str__(self):
        return f"{self.platform} {self.confirmation_code}"


class MastodonAppRegistration(models.Model):
    """Stores per-instance OAuth app registrations for Mastodon federation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    instance_url = models.URLField(max_length=500, unique=True)
    client_id = EncryptedTextField()
    client_secret = EncryptedTextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "social_accounts_mastodon_app_registration"

    def __str__(self):
        return self.instance_url


class PlatformVisibility(models.Model):
    """Site-wide toggle controlling which platforms appear on the connect page.

    Besides the social platforms, ``platform`` also holds the keys of the
    opportunity sources (``apps.radar.sources``: Shopee Affiliates and the
    announced ones), so one switch list in the administrative area covers both.
    """

    platform = models.CharField(
        max_length=30,
        choices=PlatformCredential.Platform.choices,
        unique=True,
    )
    is_visible = models.BooleanField(
        default=True,
        help_text="If unchecked, this platform is hidden from the connect page.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "social_accounts_platform_visibility"
        verbose_name = "Connect page platform"
        verbose_name_plural = "Connect page platforms"
        ordering = ["platform"]

    def __str__(self):
        return f"{self.get_platform_display()} ({'visible' if self.is_visible else 'hidden'})"

    @classmethod
    def hidden_keys(cls) -> set[str]:
        return set(cls.objects.filter(is_visible=False).values_list("platform", flat=True))

    @classmethod
    def set_visible(cls, platform: str, visible: bool) -> "PlatformVisibility":
        """Show or hide a platform or an opportunity source on every connect surface.

        Raises ValueError for a key that is neither.
        """
        from apps.radar.sources import all_sources

        if platform not in PlatformCredential.Platform.values and platform not in {s.key for s in all_sources()}:
            raise ValueError(f"Unknown platform: {platform!r}")
        row, _created = cls.objects.update_or_create(platform=platform, defaults={"is_visible": visible})
        return row

    @classmethod
    def visible_choices(cls) -> list[tuple[str, str]]:
        """``Platform.choices`` minus the platforms an admin has hidden.

        The single source for "what can be connected" — the connect page and
        the sidebar's connect shortcuts both read it, so they can't drift
        (the sidebar used to keep its own hand-maintained copy, which had
        fallen behind by two platforms). Platforms without a row default to
        visible, matching ``is_visible``.
        """
        hidden = cls.hidden_keys()
        return [(value, label) for value, label in PlatformCredential.Platform.choices if value not in hidden]

    # Platforms that connect the same network through another route; one of
    # them connected means the network is.
    SAME_NETWORK = {"instagram_login": "instagram"}

    @classmethod
    def unconnected_choices(cls, connected_platforms) -> list[tuple[str, str]]:
        """Visible platforms whose network has no connected account yet.

        Offered as "connect a channel" wherever a post's channels are picked;
        chat-only platforms publish nothing, so they are left out.
        """
        connected = {cls.SAME_NETWORK.get(p, p) for p in connected_platforms}
        return [
            (value, label)
            for value, label in cls.visible_choices()
            if cls.SAME_NETWORK.get(value, value) not in connected and value not in MESSAGING_ONLY_PLATFORMS
        ]


class AnalyticsPlatformConfig(models.Model):
    """Site-wide toggle controlling which platforms are enabled for the
    Analytics feature.

    App-review timelines for the new analytics scopes (Meta, TikTok) are
    unpredictable, so admins flip platforms on as their approvals land. If
    no rows have ``is_enabled=True`` the Analytics sidebar item is hidden
    entirely (see ``apps.common.context_processors.sidebar_context``).
    """

    platform = models.CharField(
        max_length=30,
        choices=PlatformCredential.Platform.choices,
        unique=True,
    )
    is_enabled = models.BooleanField(
        default=True,
        help_text="If unchecked, this platform is excluded from the Analytics feature.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    # Not a column: ``apps.analytics.signals.stash_previous_enabled`` writes the
    # stored ``is_enabled`` here on pre_save so post_save can spot an off → on
    # flip. Declared so that write type-checks; ``None`` means "not stashed",
    # which is what the post_save reader already defaults to.
    _previously_enabled: bool | None = None

    class Meta:
        db_table = "social_accounts_analytics_platform_config"
        verbose_name = "Analytics platform"
        verbose_name_plural = "Analytics platforms"
        ordering = ["platform"]

    def __str__(self):
        return f"{self.get_platform_display()} ({'enabled' if self.is_enabled else 'disabled'})"

    @classmethod
    def enabled_platforms(cls) -> list[str]:
        """Return the list of platform slugs with analytics enabled.

        A platform with no row counts as enabled, matching ``is_enabled``'s
        default and the fact that the admin forbids adding or deleting rows —
        so a missing row always means "nothing has been said about this
        platform", never "an admin turned it off". Without that, every platform
        slug added after the seed migration (``devto`` was the first) had its
        analytics silently switched off, which is invisible from the admin
        because the platform isn't listed there either.

        Driven off ``Platform.choices`` rather than the table, so a row left
        behind by a renamed slug can't leak into the result.
        """
        rows = dict(cls.objects.values_list("platform", "is_enabled"))
        return [value for value, _label in PlatformCredential.Platform.choices if rows.get(value, True)]
