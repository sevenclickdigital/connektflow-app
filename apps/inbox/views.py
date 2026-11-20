"""Views for the Unified Social Inbox (F-3.1)."""

import logging
from collections import Counter
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy
from django.views.decorators.http import require_GET, require_POST

from apps.composer.emoji import emoji_catalogue
from apps.contacts.models import ChannelKind
from apps.contacts.services import (
    channel_kind_for,
    contact_conversations,
    contact_for_message,
    other_channels_by_message,
)
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from . import services as inbox_services
from .forms import (
    AssignForm,
    BulkActionForm,
    InternalNoteForm,
    ReplyForm,
    SavedReplyForm,
    SentimentForm,
    SLAConfigForm,
    StatusForm,
)
from .models import (
    InboxMessage,
    InboxReply,
    InboxSLAConfig,
    InternalNote,
    SavedReply,
)

logger = logging.getLogger(__name__)

MESSAGES_PER_PAGE = 50


def _panel_context(request, workspace, message):
    """The open entry's context; on the funnel screen (``mode=funnel``) with its opportunity."""
    context = _detail_context(workspace, message, actor=request.user)
    if (request.GET.get("mode") or request.POST.get("mode")) == "funnel":
        from apps.funnels.views import opportunity_context

        context.update(opportunity_context(workspace, message))
    return context


def _detail_context(workspace, message, *, actor=None):
    """Build the full context needed for the message detail panel."""
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()
    # Variables filled in for this message, so the box receives a ready reply.
    variables = {
        "sender_name": message.sender_name,
        "account_name": message.social_account.account_name,
        # No post link is stored for inbox messages; an empty value beats a
        # literal "{post_url}" reaching the customer.
        "post_url": "",
    }
    saved_replies = [
        {"title": sr.title, "body": sr.render(variables)} for sr in SavedReply.objects.for_workspace(workspace.id)
    ]
    team_members = WorkspaceMembership.objects.filter(
        workspace=workspace,
    ).select_related("user")
    chat, draft_replies, newer = _thread(workspace, message)
    post = inbox_services.post_preview(message)
    if post and not post.get("error"):
        post["origin_label"] = dict(InboxMessage.ORIGIN_CHOICES).get(post.get("product_type"), "")
    contact = contact_for_message(message)
    return {
        "workspace": workspace,
        "message": message,
        "chat": chat,
        "draft_replies": draft_replies,
        "sla_config": sla_config,
        "saved_replies": saved_replies,
        "team_members": team_members,
        "reply_form": ReplyForm(),
        "note_form": InternalNoteForm(),
        "status_choices": InboxMessage.Status.choices,
        "sentiment_choices": InboxMessage.Sentiment.choices,
        "post_preview": post,
        "contact": contact,
        "person_channels": _person_channels(message, contact),
        "post_comments_url": _post_comments_url(workspace, message),
        "newer_message": newer,
        "reply_limit": REPLY_LIMITS.get((message.message_type == "dm", message.social_account.platform)),
        "sender_first_name": (message.sender_name or "").split(" ")[0].lstrip("@+"),
        "reply_eligibility": inbox_services.reply_eligibility(message, origin="web_inbox", actor=actor),
        **_bubble_context(message),
    }


def _post_comments_url(workspace, message) -> str:
    """Where a comment's conversation links to every comment on its post, or ""."""
    post_key = inbox_services.thread_key(message)[3]
    if not post_key:
        return ""
    path = reverse(
        "inbox:post_comments", kwargs={"workspace_id": workspace.id, "account_id": message.social_account_id}
    )
    return f"{path}?{urlencode({'post': post_key})}"


def _person_channels(message, contact):
    """The contact's conversations on each channel, for the chips in the conversation header.

    Empty unless the contact talks through more than one channel (their DM on
    two networks, a DM and comments…). ``active`` marks the open one's channel.
    """
    if contact is None:
        return []
    chips = []
    for latest in contact_conversations(contact):
        kind = channel_kind_for(latest.social_account.platform, latest.message_type)
        label = ChannelKind(kind).label if kind else latest.social_account.get_platform_display()
        if latest.message_type != InboxMessage.MessageType.DM:
            label = f"{label} · {latest.get_message_type_display()}"
        chips.append(
            {
                "message": latest,
                "kind": kind or latest.social_account.platform,
                "label": label,
                "active": (latest.social_account_id, latest.message_type)
                == (message.social_account_id, message.message_type),
            }
        )
    return chips if len(chips) > 1 else []


def _bubble_context(message):
    """What the chat bubbles need besides ``chat``: the menu on each one and the poll's token."""
    return {
        # Quote, react and copy on each bubble, where the platform has them.
        "message_actions": inbox_services.supports_message_actions(message),
        "reaction_emoji": inbox_services.REACTION_EMOJI,
        "thread_version": inbox_services.thread_version(message),
    }


def _thread(workspace, message, *, include_later=False):
    """The open entry's conversation: (chat, draft_replies, newer).

    ``include_later`` adds what the person wrote after the open entry, which is
    how the open conversation grows while it is on screen; on first opening
    those stay behind the "wrote again" link (``newer``).
    """
    replies = list(message.replies.select_related("author"))
    notes = list(message.internal_notes.select_related("author"))
    # Sent replies sit in the chronological thread; drafts (and failed
    # sends awaiting a retry) are pending work, surfaced by the composer.
    sent_replies = [r for r in replies if r.status == InboxReply.Status.SENT]
    draft_replies = [r for r in replies if r.status != InboxReply.Status.SENT]
    child_messages = list(InboxMessage.objects.filter(parent_message=message).select_related("social_account"))
    history, newer, later = _conversation(message, include_later=include_later)
    chat = _chat(workspace, message, history, sent_replies, notes, child_messages, later)
    return chat, draft_replies, newer


def _workspace_tz(workspace):
    import zoneinfo

    try:
        return zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def _chat(workspace, message, history, sent_replies, notes, child_messages, later=()):
    """The conversation as a chat, oldest first: one item per bubble.

    Each item is ``{"kind": "in" | "out" | "note", "obj", "time", "day", "tail"}``:
    the customer's messages (the earlier ones, the open one, the platform's
    replies under it and, when given, the ``later`` ones), the team's sent
    replies and the internal notes. ``time``
    is the workspace's clock; ``day`` is set on the first bubble of each day,
    for the separator. Incoming messages carry their ``attachments``, and
    ``show_name`` when someone other than the conversation's person wrote them.
    """
    from django.utils import formats
    from django.utils.translation import gettext

    tz = _workspace_tz(workspace)
    items = []
    for incoming in [*history, message, *child_messages, *later]:
        items.append(
            {
                "kind": "in",
                "obj": incoming,
                "at": incoming.received_at,
                "attachments": _attachments(workspace, incoming),
                "show_name": incoming.parent_message_id is not None and incoming.sender_name != message.sender_name,
            }
        )
    for other in [*history, *later]:
        items.extend(
            {"kind": "out", "obj": reply, "at": reply.sent_at or reply.created_at} for reply in other.sent_replies
        )
    items.extend({"kind": "out", "obj": reply, "at": reply.sent_at or reply.created_at} for reply in sent_replies)
    items.extend({"kind": "note", "obj": note, "at": note.created_at} for note in notes)
    items.sort(key=lambda item: item["at"])

    today = timezone.now().astimezone(tz).date()
    previous_day = None
    previous_kind = None
    for item in items:
        local = item["at"].astimezone(tz)
        item["time"] = local.strftime("%H:%M")
        # The first bubble of a run from one side gets the tail.
        item["tail"] = item["kind"] != previous_kind or local.date() != previous_day
        previous_kind = item["kind"]
        if local.date() != previous_day:
            previous_day = local.date()
            if previous_day == today:
                item["day"] = gettext("Today")
            elif previous_day == today - timedelta(days=1):
                item["day"] = gettext("Yesterday")
            else:
                item["day"] = formats.date_format(previous_day, "DATE_FORMAT")
    return items


# Longest reply each platform accepts, in characters: (is_dm, platform) -> limit.
# Instagram counts DM bytes (1,000); characters are what the counter can show,
# and the provider still refuses an over-long multi-byte reply with a sentence.
REPLY_LIMITS = {
    (True, "whatsapp"): 4096,
    (True, "facebook"): 2000,
    (True, "instagram"): 1000,
    (True, "instagram_login"): 1000,
    (False, "facebook"): 8000,
    (False, "instagram"): 2200,
    (False, "instagram_login"): 2200,
    (False, "youtube"): 10000,
    (False, "threads"): 500,
}

# The entry types with a button of their own on the inbox page, in its order;
# tags and reviews stay in the "Filters" panel.
TYPE_BUTTONS = (
    ("dm", pgettext_lazy("inbox type button", "Inbox")),
    ("comment", _("Comments")),
    ("mention", _("Mentions")),
)

# How much of a conversation the detail panel shows above the open message.
CONVERSATION_HISTORY = 30


def _conversation(message, *, include_later=False):
    """The other messages of the conversation ``message`` belongs to.

    The inbox lists a conversation once (inbox_services.thread_key: one person,
    channel and kind — and post, for comments), so the panel shows its messages
    together. Returns (history, newer, later): up to ``CONVERSATION_HISTORY``
    earlier inbound messages, oldest first; the newest later message, if any,
    so the panel can point to it; and, with ``include_later``, up to
    ``CONVERSATION_HISTORY`` later ones, oldest first. History and later
    messages have their sent replies prefetched.
    """
    from django.db.models import Prefetch

    with_sent_replies = Prefetch(
        "replies",
        queryset=InboxReply.objects.filter(status=InboxReply.Status.SENT).select_related("author").order_by("sent_at"),
        to_attr="sent_replies",
    )
    same_person = inbox_services.thread_messages(message).exclude(pk=message.pk)
    history = list(
        same_person.filter(received_at__lte=message.received_at)
        .prefetch_related(with_sent_replies)
        .order_by("-received_at")[:CONVERSATION_HISTORY]
    )
    history.reverse()
    after = same_person.filter(received_at__gt=message.received_at)
    newer = after.order_by("-received_at").first()
    later = []
    if include_later and newer is not None:
        later = list(
            after.select_related("social_account")
            .prefetch_related(with_sent_replies)
            .order_by("-received_at")[:CONVERSATION_HISTORY]
        )
        later.reverse()
    return history, newer, later


def _attachments(workspace, message):
    """The message's attachments, WhatsApp media pointed at our download proxy."""
    from django.urls import reverse

    attachments = message.attachments
    if message.social_account.platform != "whatsapp":
        return attachments
    raw = (message.extra or {}).get("attachments") or []
    for index, attachment in enumerate(attachments):
        if index < len(raw) and raw[index].get("media_id"):
            attachment["url"] = reverse(
                "inbox:message_media",
                kwargs={"workspace_id": workspace.id, "message_id": message.id, "index": index},
            )
    return attachments


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    has_membership = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=workspace,
    ).exists()
    if not has_membership:
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


# --- Main Feed ---


@login_required
@require_permission("use_inbox")
def inbox_feed(request, workspace_id):
    """Main inbox feed with filtering, pagination, and split-panel layout."""
    workspace = _get_workspace(request, workspace_id)
    context = feed_context(request, workspace)
    if request.htmx:
        return render(request, "inbox/partials/_message_list.html", context)
    context["emoji_categories"] = emoji_catalogue()
    return render(request, "inbox/feed.html", context)


GROUP_BY_PERSON = "person"
GROUP_BY_POST = "post"


def _list_group(request) -> str:
    """How the inbox list groups comments: per person and post, or per post.

    Chosen in the list (``group``) and kept in the session, so whoever reviews
    posts with many comments finds the list grouped by post next time.
    """
    group = request.GET.get("group")
    if group in (GROUP_BY_PERSON, GROUP_BY_POST):
        request.session["inbox_group"] = group
        return group
    return GROUP_BY_POST if request.session.get("inbox_group") == GROUP_BY_POST else GROUP_BY_PERSON


def feed_context(request, workspace, *, scope_threads=None):
    """The inbox page's list and filters, under the request's filters.

    ``scope_threads(threads) -> (threads, extra_context)`` narrows the
    conversations (one row each, keyed by ``with_thread_key``) before they are
    counted and listed; the funnel screen uses it to keep one funnel's and
    stage's conversations and to add its own context.
    """
    qs = InboxMessage.objects.for_workspace(workspace.id).select_related("social_account", "assigned_to")
    group = _list_group(request) if scope_threads is None else GROUP_BY_PERSON
    by_post = group == GROUP_BY_POST

    # Filters
    platforms = request.GET.getlist("platform")
    if platforms:
        qs = qs.filter(social_account__platform__in=platforms)

    accounts = request.GET.getlist("account")
    if accounts:
        qs = qs.filter(social_account_id__in=accounts)

    # Conversations with something unread, per type, under the channel,
    # for the type buttons.
    unread_threads = Counter(
        row["message_type"]
        for row in inbox_services.with_thread_key(qs.filter(status=InboxMessage.Status.UNREAD), by_post=by_post)
        .order_by()
        .values(*inbox_services.THREAD_FIELDS)
        .distinct()
    )
    type_counts = {key: unread_threads.get(key, 0) for key, _label in TYPE_BUTTONS}

    types = request.GET.getlist("type")
    if types:
        qs = qs.filter(message_type__in=types)

    statuses = request.GET.getlist("status")
    if statuses:
        qs = qs.filter(status__in=statuses)

    assigned = request.GET.get("assigned")
    if assigned:
        qs = qs.filter(assigned_to__isnull=True) if assigned == "unassigned" else qs.filter(assigned_to_id=assigned)

    sentiments = request.GET.getlist("sentiment")
    if sentiments:
        qs = qs.filter(sentiment__in=sentiments)

    date_from = request.GET.get("date_from")
    if date_from:
        qs = qs.filter(received_at__date__gte=date_from)

    date_to = request.GET.get("date_to")
    if date_to:
        qs = qs.filter(received_at__date__lte=date_to)

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(body__icontains=q) | Q(sender_name__icontains=q) | Q(sender_handle__icontains=q))

    # One entry per conversation: its latest message under the filters, with
    # how many messages it holds and how many of them are unread.
    keyed = inbox_services.with_thread_key(qs, by_post=by_post).order_by()
    sort = "oldest" if request.GET.get("sort") == "oldest" else "newest"
    threads = inbox_services.latest_per_thread(qs, by_post=by_post)
    extra_context = {}
    if scope_threads is not None:
        threads, extra_context = scope_threads(threads)
    threads = threads.select_related("social_account", "assigned_to", "related_post__post").order_by(
        "received_at" if sort == "oldest" else "-received_at"
    )
    total = threads.count()
    messages = list(threads[:MESSAGES_PER_PAGE])
    stats = {
        tuple(row[field] for field in inbox_services.THREAD_FIELDS): row
        for row in keyed.filter(thread_sender__in={message.thread_sender for message in messages})
        .values(*inbox_services.THREAD_FIELDS)
        .annotate(n=Count("id"), unread=Count("id", filter=Q(status=InboxMessage.Status.UNREAD)))
    }
    for message in messages:
        message.is_post_group = by_post and message.message_type == InboxMessage.MessageType.COMMENT
    post_rows = [message for message in messages if message.is_post_group]
    previews = inbox_services.cached_post_previews(post_rows)
    other_channels = other_channels_by_message(workspace.id, [m for m in messages if not m.is_post_group])
    for message in messages:
        row = stats.get(tuple(getattr(message, field) for field in inbox_services.THREAD_FIELDS), {})
        message.thread_count = row.get("n", 1)
        message.thread_unread = row.get("unread", 0)
        message.other_channels = other_channels.get(message.id, [])
        message.post_preview = previews.get(message.id)

    # SLA config for countdown display
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()

    # Team members for assignment dropdown
    team_members = WorkspaceMembership.objects.filter(
        workspace=workspace,
    ).select_related("user")

    # The channel pills (calendar/partials/_platform_pills.html) group these by platform.
    social_accounts = SocialAccount.objects.filter(
        workspace=workspace,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    ).order_by("platform")

    context = {
        "workspace": workspace,
        "inbox_messages": messages,
        "sla_config": sla_config,
        "team_members": team_members,
        "social_accounts": social_accounts,
        "total": total,
        "type_counts": type_counts,
        "type_buttons": TYPE_BUTTONS,
        "sort": sort,
        "group": group,
        "can_group": scope_threads is None,
        "active_filters": {
            "platform": platforms,
            "platforms": platforms,
            "account": accounts,
            "type": types,
            "status": statuses,
            "assigned": assigned,
            "sentiment": sentiments,
            "date_from": date_from,
            "date_to": date_to,
            "q": q,
        },
        "list_url": reverse("inbox:feed", kwargs={"workspace_id": workspace.id}),
        "detail_query": "",
    }
    context.update(extra_context)
    return context


@login_required
@require_permission("use_inbox")
@require_GET
def inbox_version(request, workspace_id):
    """A token that changes whenever a message lands in this workspace's inbox.

    The feed polls it every few seconds and refetches the list only when it
    moves, so a message that arrives by webhook shows up without a reload and
    an idle inbox costs one indexed lookup per poll.
    """
    workspace = _get_workspace(request, workspace_id)
    latest = (
        InboxMessage.objects.for_workspace(workspace.id)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    return JsonResponse({"v": latest.isoformat() if latest else ""})


# Media types the browser may render inline from our origin. Anything else is
# served as a download, so a customer cannot get HTML or SVG executed here.
_INLINE_MEDIA_PREFIXES = ("image/jpeg", "image/png", "image/webp", "image/gif", "video/", "audio/")
_MAX_MEDIA_BYTES = 25 * 1024 * 1024


@login_required
@require_permission("use_inbox")
@require_GET
def message_media(request, workspace_id, message_id, index):
    """Serve one attachment of a WhatsApp message, fetched with the account token."""
    from apps.publisher.engine import _resolve_publish_credentials
    from providers import get_provider
    from providers.exceptions import ProviderError

    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account__workspace"),
        id=message_id,
        workspace=workspace,
        social_account__platform="whatsapp",
    )
    attachments = (message.extra or {}).get("attachments") or []
    media_id = str(attachments[index].get("media_id") or "") if 0 <= index < len(attachments) else ""
    if not media_id.isdigit():
        raise Http404
    account = message.social_account
    provider = get_provider(account.platform, _resolve_publish_credentials(account))
    try:
        content, mime_type = provider.download_media(account.oauth_access_token, media_id, max_bytes=_MAX_MEDIA_BYTES)
    except ProviderError:
        logger.warning("WhatsApp media %s unavailable for message %s", media_id, message.id)
        raise Http404 from None
    inline = mime_type.startswith(_INLINE_MEDIA_PREFIXES)
    response = HttpResponse(content, content_type=mime_type if inline else "application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=3600"
    if not inline:
        response["Content-Disposition"] = f'attachment; filename="whatsapp-{media_id}"'
    return response


# --- Message Detail ---


@login_required
@require_permission("use_inbox")
def message_detail(request, workspace_id, message_id):
    """Message detail with thread, replies, notes, and reply composer."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account", "assigned_to", "related_post__post"),
        id=message_id,
        workspace=workspace,
    )

    # Opening the conversation reads all of it: unread → open.
    inbox_services.thread_messages(message).filter(status=InboxMessage.Status.UNREAD).update(
        status=InboxMessage.Status.OPEN
    )
    if message.status == InboxMessage.Status.UNREAD:
        message.status = InboxMessage.Status.OPEN

    context = _panel_context(request, workspace, message)

    if request.htmx:
        return render(request, "inbox/partials/_message_panel.html", context)
    context["emoji_categories"] = emoji_catalogue()
    return render(request, "inbox/message_detail.html", context)


POST_COMMENTS_PER_PAGE = 50
POST_COMMENT_FILTERS = ("all", "unread", "unanswered")


@login_required
@require_permission("use_inbox")
@require_GET
def post_comments(request, workspace_id, account_id):
    """One post's comments, the list grouped by post opens: the post on top and its
    comments newest first, 50 at a time, with the unread / unanswered filters and
    a search. The comments drawn are read; the others keep their status."""
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace)
    post_key = request.GET.get("post", "").strip()[:255]
    if not post_key:
        raise Http404
    comments = inbox_services.post_comments(workspace.id, account.id, post_key)
    latest = comments.select_related("social_account", "related_post__post").order_by("-received_at").first()
    if latest is None:
        raise Http404
    show = request.GET.get("show", "all")
    if show not in POST_COMMENT_FILTERS:
        show = "all"
    if show == "unread":
        comments = comments.filter(status=InboxMessage.Status.UNREAD)
    elif show == "unanswered":
        comments = comments.exclude(replies__status=InboxReply.Status.SENT)
    q = request.GET.get("q", "").strip()[:200]
    if q:
        comments = comments.filter(Q(body__icontains=q) | Q(sender_name__icontains=q) | Q(sender_handle__icontains=q))
    try:
        page = max(1, int(request.GET.get("page", "1")))
    except ValueError:
        page = 1
    start = (page - 1) * POST_COMMENTS_PER_PAGE
    rows = list(
        comments.select_related("social_account")
        .annotate(answered=Count("replies", filter=Q(replies__status=InboxReply.Status.SENT)))
        .order_by("-received_at")[start : start + POST_COMMENTS_PER_PAGE + 1]
    )
    has_more = len(rows) > POST_COMMENTS_PER_PAGE
    rows = rows[:POST_COMMENTS_PER_PAGE]
    unread = [row.pk for row in rows if row.status == InboxMessage.Status.UNREAD]
    if unread:
        InboxMessage.objects.filter(pk__in=unread, status=InboxMessage.Status.UNREAD).update(
            status=InboxMessage.Status.OPEN
        )
    query = {"post": post_key, "show": show, "q": q}
    context = {
        "workspace": workspace,
        "account": account,
        "comments": rows,
        "has_more": has_more,
        "next_query": urlencode({**query, "page": page + 1, "rows": 1}),
        "post_key": post_key,
        "marked_read": bool(unread),
        "show": show,
        "q": q,
    }
    if request.GET.get("rows"):
        return render(request, "inbox/partials/_post_comment_rows.html", context)
    post = inbox_services.post_preview(latest)
    if post and not post.get("error"):
        post["origin_label"] = dict(InboxMessage.ORIGIN_CHOICES).get(post.get("product_type"), "")
    base = InboxMessage.objects.filter(
        pk__in=inbox_services.post_comments(workspace.id, account.id, post_key).values("pk")
    )
    context.update(
        {
            "post_preview": post,
            "latest": latest,
            "total": base.count(),
            "unread_total": base.filter(status=InboxMessage.Status.UNREAD).count(),
            "panel_url": reverse(
                "inbox:post_comments", kwargs={"workspace_id": workspace.id, "account_id": account.id}
            ),
            "filters": [
                ("all", _("All")),
                ("unread", pgettext_lazy("post comments filter", "Unread")),
                ("unanswered", _("Unanswered")),
            ],
        }
    )
    return render(request, "inbox/partials/_post_panel.html", context)


# --- Reply ---


def _get_workspace_reply(workspace, reply_id):
    return get_object_or_404(
        InboxReply.objects.select_related("inbox_message", "inbox_message__social_account", "author"),
        id=reply_id,
        inbox_message__workspace=workspace,
    )


@login_required
@require_permission("reply_from_inbox")
@require_POST
def send_reply(request, workspace_id, message_id):
    """Send a reply via the platform API and record it."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid reply.", status=400)

    body = form.cleaned_data["body"]
    account = message.social_account
    try:
        quote = inbox_services.quote_for(message, request.POST.get("quote", "").strip())
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)

    # A reply is only recorded if the platform accepted it. Recording it
    # regardless would show the team a sent reply the customer never got.
    try:
        reply = inbox_services.send_reply(
            message=message, body=body, author=request.user, origin="web_inbox", quote=quote
        )
    except Exception as exc:
        logger.warning(
            "Reply failed: message_id=%s platform=%s error_type=%s %s",
            message.id,
            account.platform,
            type(exc).__name__,
            inbox_services.provider_error_detail(exc),
        )
        response = render(
            request,
            "inbox/partials/_reply_error.html",
            {
                "platform_label": account.get_platform_display(),
                "reason": inbox_services.reply_failure_reason(exc),
            },
        )
        # htmx does not swap on a 4xx/5xx, so the failure is reported as a
        # normal swap plus a header the composer reads to keep the draft text.
        response["HX-Reply-Failed"] = "1"
        return response

    sent = reply.sent_at or reply.created_at
    context = {
        "reply": reply,
        "workspace": workspace,
        "message": message,
        "time": sent.astimezone(_workspace_tz(workspace)).strftime("%H:%M"),
        "tail": True,
    }
    return render(request, "inbox/partials/_reply_item.html", context)


@login_required
@require_permission("use_inbox")
@require_GET
def message_thread(request, workspace_id, message_id):
    """The open conversation's bubbles, when they changed since the token in ``v``.

    The panel polls this every few seconds and gets 204 while nothing moved.
    What the person wrote after the open entry is included, so the chat grows
    on screen without redrawing the panel around it, and the reply being typed
    stays where it is.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account"), id=message_id, workspace=workspace
    )
    context = _bubble_context(message)
    if request.GET.get("v") == context["thread_version"]:
        return HttpResponse(status=204)
    chat, _drafts, _newer = _thread(workspace, message, include_later=True)
    return render(
        request, "inbox/partials/_thread.html", {**context, "chat": chat, "workspace": workspace, "message": message}
    )


@login_required
@require_permission("reply_from_inbox")
@require_POST
def react_message(request, workspace_id, message_id):
    """Put an emoji on a customer's chat message, or take it off (empty ``emoji``).

    Answers with the message's reaction badge; a refusal keeps the badge as it
    was and says why in a toast.
    """
    from apps.common.htmx import toast_response

    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account", "workspace"), id=message_id, workspace=workspace
    )
    try:
        inbox_services.react_to_message(
            message=message, emoji=request.POST.get("emoji", "").strip(), actor=request.user, origin="web_inbox"
        )
    except Exception as exc:
        logger.warning(
            "Reaction failed: message_id=%s platform=%s error_type=%s %s",
            message.id,
            message.social_account.platform,
            type(exc).__name__,
            inbox_services.provider_error_detail(exc),
        )
        reason = str(exc) if isinstance(exc, ValueError) else inbox_services.reply_failure_reason(exc)
        return toast_response(tone="error", title=str(_("The reaction was not sent")), body=reason)
    return render(request, "inbox/partials/_message_reaction.html", {"msg": message, "workspace": workspace})


@login_required
@require_permission("use_inbox")
@require_POST
def save_reply_draft(request, workspace_id, message_id):
    """Save a reply as a draft without sending it."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid reply.", status=400)

    try:
        inbox_services.create_reply_draft(
            message=message,
            body=form.cleaned_data["body"],
            author=request.user,
            quote=inbox_services.quote_for(message, request.POST.get("quote", "").strip()),
        )
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)

    message.refresh_from_db()
    return render(request, "inbox/partials/_message_panel.html", _panel_context(request, workspace, message))


@login_required
@require_permission("reply_from_inbox")
@require_POST
def send_reply_draft(request, workspace_id, reply_id):
    """Deliver an existing draft reply to the platform."""
    workspace = _get_workspace(request, workspace_id)
    reply = _get_workspace_reply(workspace, reply_id)
    message = reply.inbox_message

    failed = False
    try:
        inbox_services.send_reply_now(reply, actor=request.user, origin="web_inbox")
    except inbox_services.ReplyStateError as exc:
        return HttpResponse(str(exc), status=409)
    except Exception:
        # The draft is kept (now in ``failed`` state) so the team can retry
        # or discard it; the re-rendered panel shows it with failed styling.
        logger.exception("Failed to send draft reply %s", reply.id)
        failed = True

    message.refresh_from_db()
    panel = render(request, "inbox/partials/_message_panel.html", _panel_context(request, workspace, message))
    if failed:
        panel["HX-Reply-Failed"] = "1"
    return panel


@login_required
@require_permission("use_inbox")
@require_POST
def discard_reply_draft(request, workspace_id, reply_id):
    """Delete a draft (or failed) reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = _get_workspace_reply(workspace, reply_id)
    message = reply.inbox_message

    try:
        inbox_services.discard_reply_draft(reply)
    except inbox_services.ReplyStateError as exc:
        return HttpResponse(str(exc), status=409)

    message.refresh_from_db()
    return render(request, "inbox/partials/_message_panel.html", _panel_context(request, workspace, message))


# --- Internal Note ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def add_note(request, workspace_id, message_id):
    """Add an internal note to a message."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = InternalNoteForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid note.", status=400)

    note = InternalNote.objects.create(
        inbox_message=message,
        author=request.user,
        body=form.cleaned_data["body"],
    )

    context = {
        "note": note,
        "workspace": workspace,
        "message": message,
        "time": note.created_at.astimezone(_workspace_tz(workspace)).strftime("%H:%M"),
        "tail": True,
    }
    return render(request, "inbox/partials/_note_item.html", context)


# --- Assignment ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def assign_message(request, workspace_id, message_id):
    """Assign a message to a team member."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = AssignForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid assignment.", status=400)

    assigned_to_id = form.cleaned_data.get("assigned_to")
    if assigned_to_id:
        # Verify the user is a workspace member
        membership = (
            WorkspaceMembership.objects.filter(workspace=workspace, user_id=assigned_to_id)
            .select_related("user")
            .first()
        )
        if not membership:
            return HttpResponse("User is not a workspace member.", status=400)
        message.assigned_to = membership.user
    else:
        message.assigned_to = None

    # The owner is the conversation's, not one message's.
    inbox_services.thread_messages(message).update(assigned_to=message.assigned_to)

    # Notify the assignee
    if message.assigned_to and message.assigned_to != request.user:
        notify(
            user=message.assigned_to,
            event_type=EventType.NEW_INBOX_MESSAGE,
            title=f"You were assigned a {message.get_message_type_display()}",
            body=f"From {message.sender_name}: {message.body[:100]}",
            data={
                "message_id": str(message.id),
                "workspace_id": str(workspace.id),
            },
        )

    context = _panel_context(request, workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Status ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def change_status(request, workspace_id, message_id):
    """Change message status."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = StatusForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid status.", status=400)

    message.status = form.cleaned_data["status"]
    # The status is the conversation's: resolving it resolves every message in it.
    inbox_services.thread_messages(message).update(status=message.status)

    context = _panel_context(request, workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Sentiment Override ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def change_sentiment(request, workspace_id, message_id):
    """Override sentiment manually."""
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = SentimentForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid sentiment.", status=400)

    message.sentiment = form.cleaned_data["sentiment"]
    message.sentiment_source = InboxMessage.SentimentSource.MANUAL
    message.save(update_fields=["sentiment", "sentiment_source"])

    context = _panel_context(request, workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Bulk Actions ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def bulk_action(request, workspace_id):
    """Perform bulk actions on multiple messages."""
    workspace = _get_workspace(request, workspace_id)

    form = BulkActionForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Invalid bulk action.", status=400)

    message_ids = form.cleaned_data["message_ids"]
    action = form.cleaned_data["action"]
    value = form.cleaned_data.get("value", "")

    # Each listed entry stands for its whole conversation.
    thread_ids = set()
    for selected in InboxMessage.objects.filter(id__in=message_ids, workspace=workspace):
        thread_ids.update(inbox_services.thread_messages(selected).values_list("id", flat=True))
    qs = InboxMessage.objects.filter(id__in=thread_ids, workspace=workspace)

    if action == "mark_read":
        qs.filter(status=InboxMessage.Status.UNREAD).update(status=InboxMessage.Status.OPEN)
    elif action == "resolve":
        qs.exclude(status=InboxMessage.Status.ARCHIVED).update(status=InboxMessage.Status.RESOLVED)
    elif action == "archive":
        qs.update(status=InboxMessage.Status.ARCHIVED)
    elif action == "assign" and value:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user_id=value).first()
        if membership:
            qs.update(assigned_to=membership.user)

    # The page reloads the list under the filters it has on screen.
    return HttpResponse(status=204)


# --- Saved Replies ---


@login_required
@require_permission("manage_workspace_settings")
def saved_replies_list(request, workspace_id):
    """List saved replies for a workspace."""
    workspace = _get_workspace(request, workspace_id)
    replies = SavedReply.objects.for_workspace(workspace.id)

    context = {"workspace": workspace, "saved_replies": replies}
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
def saved_reply_create(request, workspace_id):
    """Create a new saved reply."""
    workspace = _get_workspace(request, workspace_id)

    if request.method == "POST":
        form = SavedReplyForm(request.POST)
        if form.is_valid():
            reply = form.save(commit=False)
            reply.workspace = workspace
            reply.created_by = request.user
            reply.save()
            return redirect("inbox:saved_replies", workspace_id=workspace.id)
    else:
        form = SavedReplyForm()

    context = {"workspace": workspace, "form": form}

    if request.htmx:
        return render(request, "inbox/partials/_saved_reply_form.html", context)
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
def saved_reply_edit(request, workspace_id, reply_id):
    """Edit an existing saved reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = get_object_or_404(SavedReply, id=reply_id, workspace=workspace)

    if request.method == "POST":
        form = SavedReplyForm(request.POST, instance=reply)
        if form.is_valid():
            form.save()
            return redirect("inbox:saved_replies", workspace_id=workspace.id)
    else:
        form = SavedReplyForm(instance=reply)

    context = {"workspace": workspace, "form": form, "saved_reply": reply}

    if request.htmx:
        return render(request, "inbox/partials/_saved_reply_form.html", context)
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def saved_reply_delete(request, workspace_id, reply_id):
    """Delete a saved reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = get_object_or_404(SavedReply, id=reply_id, workspace=workspace)
    reply.delete()
    return redirect("inbox:saved_replies", workspace_id=workspace.id)


# --- SLA Config ---


@login_required
@require_permission("manage_workspace_settings")
def sla_config(request, workspace_id):
    """Configure SLA settings for inbox."""
    workspace = _get_workspace(request, workspace_id)
    config, _created = InboxSLAConfig.objects.get_or_create(
        workspace=workspace,
        defaults={"target_response_minutes": 120, "is_active": False},
    )

    if request.method == "POST":
        form = SLAConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            return redirect("inbox:feed", workspace_id=workspace.id)
    else:
        form = SLAConfigForm(instance=config)

    context = {"workspace": workspace, "form": form, "config": config}
    return render(request, "inbox/sla_config.html", context)
