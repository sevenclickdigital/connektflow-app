"""Service layer for the Unified Social Inbox (F-3.1).

Both the HTMX views and the programmatic surfaces (the ``/api/v1/inbox``
REST router and the MCP inbox tools) go through these functions so the
three can't drift — the same rule the composer follows with
``apps.composer.services``.

A reply now has a lifecycle: it is created as a ``draft``, then a separate
send step delivers it to the platform and moves it to ``sent`` (or
``failed`` if the platform refused it). The platform-dispatch logic used
to live in ``apps/inbox/views.py``; it moved here verbatim.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from providers import get_provider
from providers.exceptions import MessagingError
from providers.meta_messaging import META_DM_PLATFORMS

from .models import InboxMessage, InboxReply, InboxSLAConfig
from .reply_policy import PROGRAMMATIC, recipient_id, reply_eligibility

logger = logging.getLogger(__name__)

# Message types answered on a comment edge rather than a messaging endpoint.
_COMMENT_LIKE_TYPES = {
    InboxMessage.MessageType.COMMENT,
    InboxMessage.MessageType.MENTION,
    InboxMessage.MessageType.REVIEW,
}

# States a reply can be sent (or re-sent) from.
_SENDABLE_STATUSES = {InboxReply.Status.DRAFT, InboxReply.Status.FAILED}


# The reactions the chat offers, the same first row WhatsApp shows.
REACTION_EMOJI = ("👍", "❤️", "😂", "😮", "😢", "🙏")

# How much of a quoted message a reply keeps to show above its own text.
QUOTE_SNIPPET_LENGTH = 280


class ReplyStateError(ValueError):
    """Raised when an operation is not valid for a reply's current status."""


def supports_message_actions(message: InboxMessage) -> bool:
    """Whether a chat message can be quoted and reacted to: WhatsApp chats only."""
    return message.message_type == InboxMessage.MessageType.DM and message.social_account.platform == "whatsapp"


def quote_for(message: InboxMessage, quoted_id) -> dict | None:
    """The snapshot a reply to ``message``'s conversation keeps of the message it quotes.

    ``quoted_id`` comes from the browser, so it only counts when it names a
    message of this same conversation. Returns None when nothing is quoted;
    raises ValueError when the id is not one this reply may quote.
    """
    from django.core.exceptions import ValidationError

    if not quoted_id:
        return None
    if not supports_message_actions(message):
        raise ValueError(_("This conversation cannot quote a message."))
    try:
        quoted = thread_messages(message).filter(pk=quoted_id).first()
    except (ValueError, ValidationError):
        quoted = None
    if quoted is None or not quoted.platform_message_id:
        raise ValueError(_("The quoted message is not part of this conversation."))
    return {
        "message_id": str(quoted.pk),
        "platform_id": quoted.platform_message_id,
        "sender_name": quoted.sender_name,
        "text": (quoted.body or "")[:QUOTE_SNIPPET_LENGTH],
    }


# ---------------------------------------------------------------------------
# Platform dispatch (moved from views.py, behaviour unchanged)
# ---------------------------------------------------------------------------


def reply_failure_reason(exc: Exception) -> str:
    """A short, actionable reason for the user.

    The platform's own error text carries internal diagnostics (trace IDs,
    raw API JSON) that mean nothing to a workspace member, so it stays in
    the log and the UI/API gets a stable sentence instead.
    """
    import httpx

    from providers.exceptions import OAuthError, RateLimitError, TokenExpiredError

    if isinstance(exc, MessagingError):
        return str(exc)
    if isinstance(exc, httpx.TransportError):
        return "Delivery could not be confirmed. Check the conversation before retrying."
    if isinstance(exc, RateLimitError):
        return "the account has hit its rate limit. Wait a few minutes and try again."
    if isinstance(exc, TokenExpiredError | OAuthError):
        return "the connection has expired. Reconnect the account in Workspace Settings."
    return "the platform rejected it. Try again, or reconnect the account if this keeps happening."


def _dispatch_to_platform(
    message: InboxMessage, body: str, *, origin=PROGRAMMATIC, actor=None, quote_platform_id: str = ""
) -> str:
    """Post ``body`` back to the platform and return the platform's reply ID.

    ``quote_platform_id`` is the platform id of the customer's message the
    reply quotes, when the person picked one. Raises if the platform refuses
    the reply, so the caller can avoid recording it as delivered when it never
    was.
    """
    from apps.publisher.engine import _resolve_publish_credentials

    account = message.social_account
    credentials = _resolve_publish_credentials(account)
    provider = get_provider(account.platform, credentials)

    # The messaging endpoints address a person, not a message, so carry the
    # sender's platform-scoped ID alongside the original payload.
    extra = dict(message.extra or {})
    recipient = recipient_id(message)
    if recipient:
        extra.setdefault("recipient_id", recipient)
    if quote_platform_id:
        extra["quote_message_id"] = quote_platform_id

    if message.message_type == InboxMessage.MessageType.MENTION:
        # A mention sits on media another account owns, which the platforms
        # answer on a different edge from a comment on our own post.
        extra["mention"] = True

    if message.message_type in _COMMENT_LIKE_TYPES:
        result = provider.reply_to_comment(
            access_token=account.oauth_access_token,
            comment_id=message.platform_message_id,
            text=body,
            extra=extra,
        )
    elif message.message_type == InboxMessage.MessageType.TAG:
        raise MessagingError(reply_eligibility(message)["reason"], platform=account.platform)
    else:
        policy = reply_eligibility(message, origin=origin, actor=actor, credentials=credentials)
        if not policy["can_send"]:
            raise MessagingError(policy["reason"], platform=account.platform)
        result = provider.reply_to_message(
            access_token=account.oauth_access_token,
            message_id=message.platform_message_id,
            text=body,
            extra=extra,
            human_agent=policy["human_agent"],
        )

    if message.message_type == "dm" and account.platform in META_DM_PLATFORMS and not result.platform_message_id:
        raise MessagingError(
            "Delivery could not be confirmed. Check the conversation before retrying.", platform=account.platform
        )
    return result.platform_message_id


def _apply_post_send_side_effects(message: InboxMessage) -> None:
    """Resolve or open the message after a reply goes out, per SLA config."""
    sla_config = InboxSLAConfig.objects.filter(workspace=message.workspace, is_active=True).first()
    if sla_config and sla_config.auto_resolve_on_reply:
        if message.status != InboxMessage.Status.RESOLVED:
            message.status = InboxMessage.Status.RESOLVED
            message.save(update_fields=["status"])
    elif message.status == InboxMessage.Status.UNREAD:
        message.status = InboxMessage.Status.OPEN
        message.save(update_fields=["status"])


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


def create_reply_draft(*, message: InboxMessage, body: str, author=None, quote: dict | None = None) -> InboxReply:
    """Create a ``draft`` reply against ``message``. Not sent anywhere.

    ``quote`` is the snapshot from ``quote_for`` when the reply quotes one of
    the customer's messages.
    """
    body = (body or "").strip()
    if not body:
        raise ValueError("Reply body cannot be empty.")
    return InboxReply.objects.create(
        inbox_message=message,
        author=author,
        body=body,
        status=InboxReply.Status.DRAFT,
        extra={"quote": quote} if quote else {},
    )


def _lock_reply(reply: InboxReply) -> None:
    """Refresh the caller's instance while locking the row for this transaction."""
    try:
        reply.refresh_from_db(from_queryset=InboxReply.objects.select_for_update())
    except InboxReply.DoesNotExist as exc:
        raise ReplyStateError("This reply has been discarded.") from exc


@transaction.atomic
def update_reply_draft(reply: InboxReply, *, body: str) -> InboxReply:
    """Edit a draft (or failed) reply's body."""
    _lock_reply(reply)
    if reply.status not in _SENDABLE_STATUSES:
        raise ReplyStateError(f"A {reply.get_status_display().lower()} reply cannot be edited.")
    body = (body or "").strip()
    if not body:
        raise ValueError("Reply body cannot be empty.")
    reply.body = body
    reply.save(update_fields=["body", "updated_at"])
    return reply


@transaction.atomic
def discard_reply_draft(reply: InboxReply) -> None:
    """Delete a draft (or failed) reply. Sent replies are permanent."""
    _lock_reply(reply)
    if reply.status not in _SENDABLE_STATUSES:
        raise ReplyStateError(f"A {reply.get_status_display().lower()} reply cannot be discarded.")
    reply.delete()


def send_reply_now(reply: InboxReply, *, actor=None, origin=PROGRAMMATIC) -> InboxReply:
    """Deliver an existing draft/failed reply to the platform.

    On a platform refusal the row is kept and moved to ``failed`` with a
    human-readable ``send_error`` so the team can retry; the underlying
    exception is re-raised for the caller to shape into its own error.
    Providers without a reply API retain the historical local-record behavior,
    except Meta DMs: those must be confirmed by the platform before becoming sent.
    """
    failure = None
    with transaction.atomic():
        # Keep the lock through delivery and persistence. Every competing send,
        # edit or discard must check the latest state after acquiring this lock.
        _lock_reply(reply)
        if reply.status not in _SENDABLE_STATUSES:
            raise ReplyStateError(f"A {reply.get_status_display().lower()} reply cannot be sent again.")

        message = reply.inbox_message
        if actor is not None and reply.author_id is None:
            reply.author = actor

        try:
            platform_reply_id = _dispatch_to_platform(
                message,
                reply.body,
                origin=origin,
                actor=actor,
                quote_platform_id=((reply.extra or {}).get("quote") or {}).get("platform_id", ""),
            )
        except Exception as exc:
            if isinstance(exc, NotImplementedError) and not (
                message.message_type == "dm" and message.social_account.platform in META_DM_PLATFORMS
            ):
                platform_reply_id = ""
            else:
                if isinstance(exc, NotImplementedError):
                    exc = MessagingError(
                        "This connection cannot send DMs. Reconnect the account or check its configuration."
                    )
                logger.warning(
                    "Inbox reply failed: reply_id=%s platform=%s error_type=%s",
                    reply.id,
                    message.social_account.platform,
                    type(exc).__name__,
                )
                reply.status = InboxReply.Status.FAILED
                reply.send_error = reply_failure_reason(exc)
                reply.save(update_fields=["status", "send_error", "author", "updated_at"])
                failure = exc

        if failure is None:
            reply.status = InboxReply.Status.SENT
            reply.platform_reply_id = platform_reply_id
            reply.send_error = ""
            reply.sent_at = timezone.now()
            reply.save(update_fields=["status", "platform_reply_id", "send_error", "sent_at", "author", "updated_at"])

    # Raising inside atomic would roll back the failed status and its reason.
    if failure is not None:
        raise failure
    # SLA bookkeeping must not roll back a reply already delivered externally.
    try:
        _apply_post_send_side_effects(message)
    except Exception:
        logger.exception("Inbox reply %s was sent, but updating the message status failed", reply.id)
    return reply


def send_reply(
    *, message: InboxMessage, body: str, author=None, origin=PROGRAMMATIC, quote: dict | None = None
) -> InboxReply:
    """Create a reply and send it in one step (the classic composer flow).

    Refused Meta DM replies are retained as failed drafts for human review.
    Other providers retain their existing failed-row cleanup behavior.
    """
    with transaction.atomic():
        reply = create_reply_draft(message=message, body=body, author=author, quote=quote)
    try:
        return send_reply_now(reply, actor=author, origin=origin)
    except Exception:
        if not (message.message_type == "dm" and message.social_account.platform in META_DM_PLATFORMS):
            InboxReply.objects.filter(pk=reply.pk, status=InboxReply.Status.FAILED).delete()
        raise


def react_to_message(*, message: InboxMessage, emoji: str, actor=None, origin=PROGRAMMATIC) -> InboxMessage:
    """Put ``emoji`` on a customer's chat message, or take the reaction off with ``""``.

    The reaction is recorded on the message (``extra["team_reaction"]``) only
    after the platform accepted it. It is a message to the platform, so it
    follows the conversation's reply window, judged by its latest message.
    """
    from apps.publisher.engine import _resolve_publish_credentials

    if not supports_message_actions(message):
        raise MessagingError(_("Reactions are only available in WhatsApp conversations."))
    if emoji and emoji not in REACTION_EMOJI:
        raise ValueError(_("That reaction is not available."))
    account = message.social_account
    credentials = _resolve_publish_credentials(account)
    latest = thread_messages(message).order_by("-received_at").first() or message
    policy = reply_eligibility(latest, origin=origin, actor=actor, credentials=credentials)
    if not policy["can_send"]:
        raise MessagingError(policy["reason"], platform=account.platform)
    get_provider(account.platform, credentials).react_to_message(
        account.oauth_access_token, message.platform_message_id, emoji, recipient_id(message)
    )
    extra = dict(message.extra or {})
    if emoji:
        extra["team_reaction"] = emoji
    else:
        extra.pop("team_reaction", None)
    message.extra = extra
    message.save(update_fields=["extra"])
    return message


def fill_instagram_mention(message_id) -> bool:
    """Fetch the text, author and link of an Instagram mention filed as a placeholder.

    Idempotent: a message that no longer carries the placeholder is left alone,
    so a re-run never overwrites a body a person already read. A refused lookup
    keeps the placeholder, which already tells the team to open the post, and
    is logged — Meta refuses it when comments are off on the post.
    """
    from apps.publisher.engine import _resolve_publish_credentials
    from providers.exceptions import ProviderError
    from providers.instagram import InstagramProvider

    from .sentiment import analyze_sentiment
    from .webhooks import INSTAGRAM_MENTION_PLACEHOLDER

    message = (
        InboxMessage.objects.select_related("social_account")
        .filter(pk=message_id, message_type=InboxMessage.MessageType.MENTION, social_account__platform="instagram")
        .first()
    )
    if not message or message.body != INSTAGRAM_MENTION_PLACEHOLDER:
        return False
    account = message.social_account
    extra = dict(message.extra or {})
    provider = get_provider(account.platform, _resolve_publish_credentials(account))
    if not isinstance(provider, InstagramProvider):
        return False
    try:
        mention = provider.get_mention(
            account.oauth_access_token,
            media_id=str(extra.get("media_id") or ""),
            comment_id=str(extra.get("comment_id") or ""),
        )
    except ProviderError as exc:
        logger.warning("Could not read Instagram mention %s: %s", message.platform_message_id, exc)
        return False
    if not mention["text"]:
        return False

    message.body = mention["text"]
    if message.sentiment_source == InboxMessage.SentimentSource.AUTO:
        message.sentiment = analyze_sentiment(message.body)
    if mention["username"]:
        message.sender_name = mention["username"]
        message.sender_handle = mention["username"]
    if mention["permalink"]:
        extra["permalink"] = mention["permalink"]
    message.extra = extra
    message.save(update_fields=["body", "sentiment", "sender_name", "sender_handle", "extra"])
    return True


# ---------------------------------------------------------------------------
# Conversations: one list entry per person, channel and kind
# ---------------------------------------------------------------------------

# The fields a conversation is keyed on. Direct messages, comments and
# mentions stay apart (the type); comments also stay apart per post, since
# each post is its own public thread.
THREAD_FIELDS = ("social_account_id", "message_type", "thread_sender", "thread_post")


def with_thread_key(qs):
    """Annotate ``thread_sender`` and ``thread_post`` on inbox messages."""
    from django.db.models import Case, CharField, Value, When
    from django.db.models.fields.json import KeyTextTransform
    from django.db.models.functions import Coalesce, NullIf

    return qs.annotate(
        thread_sender=Coalesce(NullIf("sender_handle", Value("")), "sender_name"),
        thread_post=Case(
            When(
                message_type=InboxMessage.MessageType.COMMENT,
                then=Coalesce(
                    NullIf(KeyTextTransform("stored_post_id", "extra"), Value("")),
                    NullIf(KeyTextTransform("post_id", "extra"), Value("")),
                    Value(""),
                ),
            ),
            default=Value(""),
            output_field=CharField(),
        ),
    )


def latest_per_thread(qs):
    """The latest message of each conversation among ``qs``, keyed by ``with_thread_key``."""
    latest_ids = [
        row["id"]
        for row in with_thread_key(qs)
        .order_by(*THREAD_FIELDS, "-received_at")
        .distinct(*THREAD_FIELDS)
        .values("id", *THREAD_FIELDS)
    ]
    return with_thread_key(InboxMessage.objects.filter(id__in=latest_ids))


def thread_key(message: InboxMessage) -> tuple:
    """The conversation ``message`` belongs to, as ``THREAD_FIELDS`` values."""
    extra = message.extra or {}
    post = ""
    if message.message_type == InboxMessage.MessageType.COMMENT:
        post = str(extra.get("stored_post_id") or extra.get("post_id") or "")
    return (message.social_account_id, message.message_type, message.sender_handle or message.sender_name, post)


def thread_messages(message: InboxMessage):
    """Every message of ``message``'s conversation, in its workspace."""
    account_id, message_type, sender, post = thread_key(message)
    return with_thread_key(
        InboxMessage.objects.filter(
            workspace_id=message.workspace_id, social_account_id=account_id, message_type=message_type
        )
    ).filter(thread_sender=sender, thread_post=post)


# ---------------------------------------------------------------------------
# The post a comment hangs off
# ---------------------------------------------------------------------------

POST_PREVIEW_TTL = 6 * 60 * 60


def post_preview(message: InboxMessage) -> dict | None:
    """What the entry shows of its post: caption, image, link and origin.

    A post published through Connekt Flow is read locally; a tag or mention shows
    what the sync recorded of the other account's post; a comment's post is read
    from the platform once and cached, so the inbox shows it whoever published
    it. ``{"error": True}`` when the platform refused the read.
    """
    from django.core.cache import cache

    from providers.exceptions import ProviderError

    if message.related_post_id:
        return _local_post_preview(message)
    extra = message.extra or {}
    if message.message_type in (InboxMessage.MessageType.TAG, InboxMessage.MessageType.MENTION):
        # Another account's post: what the sync recorded of it is all there is.
        if not (message.post_url or extra.get("post_image")):
            return None
        return {
            "caption": message.body,
            "image_url": extra.get("post_image") or "",
            "url": message.post_url,
            "product_type": extra.get("post_origin") or "",
        }
    post_id = str(extra.get("stored_post_id") or extra.get("post_id") or "")
    if message.message_type != InboxMessage.MessageType.COMMENT or not post_id:
        return None
    account = message.social_account
    key = f"inbox:post-preview:{account.id}:{post_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    from apps.publisher.engine import _resolve_publish_credentials

    raw_id = str(extra.get("post_id") or post_id)
    try:
        provider = get_provider(account.platform, _resolve_publish_credentials(account))
        preview = provider.get_post_preview(account.oauth_access_token, raw_id)
    except ProviderError as exc:
        logger.warning("Post %s of inbox message %s could not be read: %s", raw_id, message.id, exc)
        return {"error": True, "url": message.post_url}
    if preview:
        preview = {**preview, "url": preview.get("url") or message.post_url}
        _remember_post_origin(message, post_id, preview.get("product_type") or "")
    cache.set(key, preview or {}, POST_PREVIEW_TTL)
    return preview


def _remember_post_origin(message: InboxMessage, post_id: str, origin: str) -> None:
    """Keep the post's origin on its comments, so the list shows it without a platform read."""
    if not origin:
        return
    comments = InboxMessage.objects.filter(
        workspace_id=message.workspace_id,
        social_account_id=message.social_account_id,
        message_type=InboxMessage.MessageType.COMMENT,
        extra__stored_post_id=post_id,
    )
    for comment in comments:
        if (comment.extra or {}).get("post_origin") == origin:
            continue
        comment.extra = {**(comment.extra or {}), "post_origin": origin}
        comment.save(update_fields=["extra"])


def _local_post_preview(message: InboxMessage) -> dict:
    platform_post = message.related_post
    assert platform_post is not None  # post_preview only calls this with a related post
    post = platform_post.post
    first = post.media_attachments.select_related("media_asset").first()
    image_url = ""
    if first:
        asset = first.media_asset
        image_url = asset.thumbnail.url if asset.thumbnail else ("" if asset.is_video else asset.file.url)
    origin = InboxMessage.FORMAT_ORIGINS.get(platform_post.format, "")
    return {
        "caption": post.caption or "",
        "image_url": image_url,
        "url": message.post_url,
        "product_type": origin,
        "post_id": post.id,
    }


def unread_count(workspace_id) -> int:
    """Unread inbox messages of a workspace: the sidebar badge and the dashboard's summary."""
    return InboxMessage.objects.for_workspace(workspace_id).filter(status=InboxMessage.Status.UNREAD).count()
