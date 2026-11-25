"""What the support area does: tickets, the live chat and its queue.

Views parse the request and call these; the rules live here.

**Who sees what.** A customer only ever reaches their own ticket or chat
(``Ticket.requester`` / ``Chat.visitor``): the customer views look them up
with that filter, never by id alone. Team notes (``is_internal``) and the
visitor's device and pages are left out of everything built for the
customer (:func:`visitor_state`, the ticket page's queryset).

**The queue.** A chat starts ``queued``; every agent sees the queue in the
console and one takes it (:func:`accept`, which only one can win). The
visitor is told their place in it. With nobody on the console
(:func:`agents_online`), a first message becomes a ticket instead, and a
queued chat whose visitor left becomes one after :data:`ABANDONED_AFTER`
(:func:`convert_abandoned`, run by the worker), so no question is lost.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Max, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.media_library.validators import image_storage_filename

from . import devices
from .models import AgentPresence, Chat, ChatMessage, ChatPageVisit, Ticket, TicketMessage

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4000
MAX_CHAT_MESSAGE_LENGTH = 2000
MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024
# An agent counts as online while their console polled within this window.
ONLINE_WINDOW = timedelta(seconds=45)
# A visitor counts as gone when their widget stopped polling for this long.
VISITOR_GONE_AFTER = timedelta(seconds=30)
# A queued chat whose visitor is gone this long becomes a ticket.
ABANDONED_AFTER = timedelta(minutes=10)
# Someone counts as typing for this long after their last keystroke report.
# The widget and the console report at most every few seconds while typing
# and poll every 3 s, so the other side sees it with a delay of a few seconds.
TYPING_WINDOW = timedelta(seconds=5)


class SupportError(ValueError):
    """Something the person asked cannot be done; the message says why, in their language."""


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------


def clean_body(body: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    body = (body or "").strip()
    if not body:
        raise SupportError(_("Write a message."))
    if len(body) > limit:
        raise SupportError(_("The message is too long (up to %(limit)s characters).") % {"limit": limit})
    return body


def clean_attachment(attachment):
    """The uploaded image, renamed for storage; None without one. Raises SupportError for anything else."""
    if not attachment:
        return None
    if attachment.size > MAX_ATTACHMENT_SIZE:
        raise SupportError(_("The image is larger than 5 MB."))
    storage_name = image_storage_filename(attachment)
    if not storage_name:
        raise SupportError(_("Use a PNG, JPEG, WebP or GIF image."))
    attachment.name = storage_name
    return attachment


def open_ticket(
    *,
    requester,
    kind: str,
    subject: str,
    body: str,
    attachment=None,
    organization=None,
    workspace=None,
    source: str = Ticket.Source.FORM,
) -> Ticket:
    """Open a ticket with its first message and tell the team (after the commit)."""
    subject = " ".join((subject or "").split())[:200]
    if not subject:
        raise SupportError(_("Write a subject."))
    if kind not in Ticket.Kind.values:
        raise SupportError(_("Choose what the request is about."))
    body = clean_body(body)
    attachment = clean_attachment(attachment)
    with transaction.atomic():
        ticket = Ticket.objects.create(
            requester=requester,
            kind=kind,
            subject=subject,
            organization=organization,
            workspace=workspace,
            source=source,
            requester_seen_at=timezone.now(),
        )
        TicketMessage.objects.create(ticket=ticket, author=requester, body=body, attachment=attachment or "")
    _after_commit(lambda: _emails().send_ticket_opened_to_team(ticket))
    return ticket


def reply(ticket: Ticket, *, author, body: str, from_team: bool, internal: bool = False, attachment=None):
    """Add a message to ``ticket`` and move its status the way the reply implies.

    A customer reply reopens a ticket that was waiting for them or resolved.
    A public team reply puts it in "waiting for your reply" and e-mails the
    requester; an internal note changes nothing the customer sees.
    """
    body = clean_body(body)
    attachment = clean_attachment(attachment)
    internal = internal and from_team
    now = timezone.now()
    with transaction.atomic():
        message = TicketMessage.objects.create(
            ticket=ticket,
            author=author,
            body=body,
            from_team=from_team,
            is_internal=internal,
            attachment=attachment or "",
        )
        fields: dict[str, Any] = {"last_activity_at": now}
        if not from_team:
            fields["requester_seen_at"] = now
            if ticket.status in (Ticket.Status.WAITING_CUSTOMER, Ticket.Status.RESOLVED):
                fields["status"] = Ticket.Status.OPEN
        elif not internal and ticket.status != Ticket.Status.RESOLVED:
            fields["status"] = Ticket.Status.WAITING_CUSTOMER
        if from_team and ticket.assignee_id is None:
            fields["assignee"] = author
        Ticket.objects.filter(pk=ticket.pk).update(**fields)
        for name, value in fields.items():
            setattr(ticket, name, value)
    if from_team and not internal:
        _after_commit(lambda: _emails().send_ticket_reply(ticket, message))
    return message


def set_status(ticket: Ticket, status: str) -> None:
    if status not in Ticket.Status.values:
        raise SupportError(_("Unknown status."))
    ticket.status = status
    ticket.save(update_fields=["status"])


def assign(ticket: Ticket, agent) -> None:
    ticket.assignee = agent
    ticket.save(update_fields=["assignee"])


def tickets_of(user):
    """The person's own tickets, with ``has_news`` when the team answered since they last looked."""
    return (
        Ticket.objects.filter(requester=user)
        .annotate(
            last_team_reply_at=Max(
                "messages__created_at", filter=Q(messages__from_team=True, messages__is_internal=False)
            )
        )
        .order_by("-last_activity_at")
    )


def has_news(ticket) -> bool:
    last = getattr(ticket, "last_team_reply_at", None)
    return bool(last and (ticket.requester_seen_at is None or last > ticket.requester_seen_at))


def customer_messages(ticket: Ticket):
    """What the requester sees of the conversation: everything but the team's notes."""
    return ticket.messages.filter(is_internal=False).select_related("author")


def mark_seen_by_requester(ticket: Ticket) -> None:
    ticket.requester_seen_at = timezone.now()
    Ticket.objects.filter(pk=ticket.pk).update(requester_seen_at=ticket.requester_seen_at)


# ---------------------------------------------------------------------------
# Live chat: the visitor's side
# ---------------------------------------------------------------------------


def open_chat_of(user) -> Chat | None:
    return Chat.objects.filter(visitor=user).exclude(status=Chat.Status.CLOSED).select_related("agent").first()


def agents_online() -> bool:
    return AgentPresence.objects.filter(last_seen_at__gte=timezone.now() - ONLINE_WINDOW).exists()


def queue_position(chat: Chat) -> int:
    """1 for the next chat to be taken; 0 when the chat is not waiting."""
    if chat.status != Chat.Status.QUEUED:
        return 0
    return Chat.objects.filter(status=Chat.Status.QUEUED, created_at__lt=chat.created_at).count() + 1


def visitor_writes(
    user, body: str, *, user_agent: str = "", page: dict | None = None, organization=None, workspace=None
) -> Chat | Ticket:
    """The visitor's message: into their open chat, a new queued chat, or, with nobody online, a new ticket.

    ``page`` is what the widget reports about where it was written
    (``path``, ``title``, ``screen``, ``language``); the device comes from
    ``user_agent``.
    """
    body = clean_body(body, MAX_CHAT_MESSAGE_LENGTH)
    chat = open_chat_of(user)
    if chat is None:
        if not agents_online():
            return open_ticket(
                requester=user,
                kind=Ticket.Kind.SUPPORT,
                subject=body.splitlines()[0][:120],
                body=body,
                organization=organization,
                workspace=workspace,
                source=Ticket.Source.CHAT,
            )
        chat = _start_chat(user, user_agent=user_agent, page=page or {}, organization=organization, workspace=workspace)
    ChatMessage.objects.create(chat=chat, author=user, body=body)
    # The message went out: they are no longer typing it.
    Chat.objects.filter(pk=chat.pk).update(visitor_seen_at=timezone.now(), visitor_typing_at=None)
    return chat


def _start_chat(user, *, user_agent, page, organization, workspace) -> Chat:
    device = devices.describe(user_agent)
    try:
        with transaction.atomic():
            chat = Chat.objects.create(
                visitor=user,
                organization=organization,
                workspace=workspace,
                browser=device.browser,
                operating_system=device.operating_system,
                device=device.kind,
                screen=str(page.get("screen", ""))[:20],
                browser_language=str(page.get("language", ""))[:20],
                user_agent=(user_agent or "")[:400],
            )
    except IntegrityError:
        # Two sends at once: the other one opened the chat.
        existing = open_chat_of(user)
        if existing is None:
            raise
        return existing
    record_visit(chat, page.get("path", ""), page.get("title", ""))
    return chat


def record_visit(chat: Chat, path: str, title: str = "") -> bool:
    """Note that the visitor is on ``path`` now; a reload of the same page is not a new visit."""
    path = (path or "")[:500]
    title = " ".join((title or "").split())[:200]
    if not path.startswith("/") or path == chat.current_path:
        return False
    ChatPageVisit.objects.create(chat=chat, path=path, title=title)
    Chat.objects.filter(pk=chat.pk).update(current_path=path, current_title=title)
    chat.current_path, chat.current_title = path, title
    return True


def visitor_closes(chat: Chat) -> None:
    _close(chat)
    # The visitor knows it ended: nothing to tell them on the next poll.
    Chat.objects.filter(pk=chat.pk).update(visitor_seen_at=timezone.now())


def visitor_state(user, *, after: int = 0) -> dict:
    """What the widget shows: the open chat (status, place in the queue, agent), its new messages, who is online.

    Marks the visitor as present and as having read the agent's messages.
    Nothing about devices, pages or the team's notes.
    """
    chat = open_chat_of(user)
    state: dict[str, Any] = {"online": agents_online(), "chat": None, "messages": [], "ended": None}
    if chat is None:
        state["ended"] = _just_ended(user, after)
        return state
    now = timezone.now()
    unread = chat.messages.filter(from_team=True, created_at__gt=chat.visitor_seen_at or chat.created_at).count()
    Chat.objects.filter(pk=chat.pk).update(visitor_seen_at=now)
    state["chat"] = {
        "status": chat.status,
        "position": queue_position(chat),
        "agent": chat.agent.greeting_name if chat.agent else "",
        "unread": unread,
        "typing": _is_typing(chat.agent_typing_at),
    }
    state["messages"] = [
        _message_json(message) for message in chat.messages.filter(id__gt=after).select_related("author")
    ]
    return state


def _just_ended(user, after: int) -> dict | None:
    """The chat the team closed since the visitor last looked, told once: whether it became a ticket, and its last messages."""
    chat = (
        Chat.objects.filter(visitor=user, status=Chat.Status.CLOSED)
        .filter(Q(visitor_seen_at__isnull=True) | Q(closed_at__gt=F("visitor_seen_at")))
        .order_by("-closed_at")
        .first()
    )
    if chat is None:
        return None
    Chat.objects.filter(pk=chat.pk).update(visitor_seen_at=timezone.now())
    ended: dict[str, Any] = {"ticket": None, "messages": []}
    if chat.ticket_id:
        ended["ticket"] = {
            "number": chat.ticket_id,
            "url": reverse("support:ticket", kwargs={"ticket_id": chat.ticket_id}),
        }
    if after:
        ended["messages"] = [_message_json(m) for m in chat.messages.filter(id__gt=after).select_related("author")]
    return ended


def _message_json(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "body": message.body,
        "from_team": message.from_team,
        "author": message.author.greeting_name if message.author else "",
        "at": timezone.localtime(message.created_at).strftime("%H:%M"),
    }


def with_question(chats):
    """``chats`` annotated with ``message_count`` and ``question`` (the first thing the visitor wrote), in one query."""
    first = ChatMessage.objects.filter(chat=OuterRef("pk"), from_team=False).order_by("id").values("body")[:1]
    return chats.annotate(message_count=Count("messages"), question=Subquery(first))


def chats_of(user):
    """The person's chats, newest first."""
    return with_question(Chat.objects.filter(visitor=user).select_related("agent")).order_by("-created_at")


# ---------------------------------------------------------------------------
# Live chat: the team's side
# ---------------------------------------------------------------------------


def mark_presence(agent) -> None:
    AgentPresence.objects.update_or_create(user=agent, defaults={"last_seen_at": timezone.now()})


def accept(chat_id, agent) -> Chat | None:
    """Take a queued chat. None when it is no longer in the queue (another agent was faster, the visitor left)."""
    with transaction.atomic():
        chat = Chat.objects.select_for_update().filter(pk=chat_id, status=Chat.Status.QUEUED).first()
        if chat is None:
            return None
        chat.status = Chat.Status.ACTIVE
        chat.agent = agent
        chat.accepted_at = chat.agent_seen_at = timezone.now()
        chat.save(update_fields=["status", "agent", "accepted_at", "agent_seen_at"])
    return chat


def agent_writes(chat: Chat, agent, body: str) -> ChatMessage:
    if chat.status != Chat.Status.ACTIVE or chat.agent_id != agent.pk:
        raise SupportError(_("Take the chat before answering it."))
    message = ChatMessage.objects.create(
        chat=chat, author=agent, from_team=True, body=clean_body(body, MAX_CHAT_MESSAGE_LENGTH)
    )
    Chat.objects.filter(pk=chat.pk).update(agent_seen_at=timezone.now(), agent_typing_at=None)
    return message


def visitor_types(chat: Chat) -> None:
    """The visitor is typing in their open chat."""
    if chat.status != Chat.Status.CLOSED:
        Chat.objects.filter(pk=chat.pk).update(visitor_typing_at=timezone.now())


def agent_types(chat: Chat, agent) -> None:
    """The agent is typing in a chat they are answering; ignored in anyone else's."""
    if chat.status == Chat.Status.ACTIVE and chat.agent_id == agent.pk:
        Chat.objects.filter(pk=chat.pk).update(agent_typing_at=timezone.now())


def _is_typing(when) -> bool:
    return bool(when and when >= timezone.now() - TYPING_WINDOW)


def agent_closes(chat: Chat) -> None:
    _close(chat)


def _close(chat: Chat) -> None:
    if chat.status == Chat.Status.CLOSED:
        return
    chat.status = Chat.Status.CLOSED
    chat.closed_at = timezone.now()
    chat.save(update_fields=["status", "closed_at"])


def convert_to_ticket(chat: Chat, *, agent=None, subject: str = "", kind: str = Ticket.Kind.SUPPORT, note: str = ""):
    """Close the chat into a ticket that carries its whole conversation; ``note`` is kept as a team note.

    The ticket goes to ``agent`` (in progress) or, without one, waits as open.
    """
    first = chat.messages.filter(from_team=False).first()
    subject = " ".join((subject or "").split()) or (first.body.splitlines()[0][:120] if first else _("Live chat"))
    with transaction.atomic():
        ticket = Ticket.objects.create(
            requester=chat.visitor,
            organization=chat.organization,
            workspace=chat.workspace,
            kind=kind if kind in Ticket.Kind.values else Ticket.Kind.SUPPORT,
            subject=subject[:200],
            source=Ticket.Source.CHAT,
            assignee=agent,
            status=Ticket.Status.IN_PROGRESS if agent else Ticket.Status.OPEN,
            requester_seen_at=timezone.now(),
        )
        originals = list(chat.messages.all())
        copies = TicketMessage.objects.bulk_create(
            [TicketMessage(ticket=ticket, author_id=m.author_id, from_team=m.from_team, body=m.body) for m in originals]
        )
        # auto_now_add stamped every copy with now; keep the time each was written in the chat.
        for original, copy in zip(originals, copies, strict=True):
            copy.created_at = original.created_at
        TicketMessage.objects.bulk_update(copies, ["created_at"])
        if note.strip():
            TicketMessage.objects.create(
                ticket=ticket, author=agent, from_team=True, is_internal=True, body=note.strip()
            )
        chat.ticket = ticket
        chat.status = Chat.Status.CLOSED
        chat.closed_at = timezone.now()
        chat.save(update_fields=["ticket", "status", "closed_at"])
    if agent is None:
        _after_commit(lambda: _emails().send_ticket_opened_to_team(ticket))
    return ticket


def convert_abandoned() -> int:
    """Turn every queued chat whose visitor left more than ABANDONED_AFTER ago into a ticket."""
    cutoff = timezone.now() - ABANDONED_AFTER
    stale = Chat.objects.filter(status=Chat.Status.QUEUED).filter(
        Q(visitor_seen_at__lt=cutoff) | Q(visitor_seen_at__isnull=True, created_at__lt=cutoff)
    )
    converted = 0
    for chat in stale.select_related("visitor"):
        convert_to_ticket(chat)
        converted += 1
    return converted


def console_feed(agent) -> dict:
    """The console's lists: the queue, the agent's own chats (with unread counts) and the others in progress."""
    now = timezone.now()
    gone = now - VISITOR_GONE_AFTER
    open_chats = (
        Chat.objects.exclude(status=Chat.Status.CLOSED)
        .select_related("visitor", "agent", "organization")
        .annotate(last_message_id=Max("messages__id"))
    )
    unread = dict(
        Chat.objects.filter(agent=agent, status=Chat.Status.ACTIVE)
        .annotate(
            n=Count(
                "messages",
                filter=Q(messages__from_team=False)
                & (Q(agent_seen_at__isnull=True) | Q(messages__created_at__gt=F("agent_seen_at"))),
            )
        )
        .values_list("pk", "n")
    )
    queue, mine, others = [], [], []
    for chat in open_chats:
        item = {
            "id": str(chat.pk),
            "visitor": chat.visitor.display_name,
            "organization": chat.organization.name if chat.organization else "",
            "since": timezone.localtime(chat.created_at).strftime("%H:%M"),
            "waiting_minutes": int((now - chat.created_at).total_seconds() // 60),
            "present": bool(chat.visitor_seen_at and chat.visitor_seen_at >= gone),
            "last_message_id": chat.last_message_id or 0,
            "page": short_title(chat.current_title) or chat.current_path,
        }
        if chat.status == Chat.Status.QUEUED:
            first = chat.messages.filter(from_team=False).first()
            item["preview"] = first.body[:140] if first else ""
            queue.append(item)
        elif chat.agent_id == agent.pk:
            item["unread"] = unread.get(chat.pk, 0)
            item["typing"] = _is_typing(chat.visitor_typing_at)
            mine.append(item)
        else:
            item["agent"] = chat.agent.greeting_name if chat.agent else ""
            others.append(item)
    return {"queue": queue, "mine": mine, "others": others}


_PRODUCT_SUFFIX = re.compile(r"\s+[-–|]\s+Connekt Flow$")


def short_title(title: str) -> str:
    """A page's title without the product's name at its end ("Calendar - Connekt Flow" → "Calendar")."""
    return _PRODUCT_SUFFIX.sub("", title or "")


def console_chat(chat: Chat, agent, *, after: int = 0) -> dict:
    """One chat for the console: its new messages, the visitor and their device, and the pages they went through."""
    if chat.agent_id == agent.pk:
        Chat.objects.filter(pk=chat.pk).update(agent_seen_at=timezone.now())
    visitor = chat.visitor
    gone = timezone.now() - VISITOR_GONE_AFTER
    return {
        "id": str(chat.pk),
        "status": chat.status,
        "mine": chat.agent_id == agent.pk,
        "agent": chat.agent.greeting_name if chat.agent else "",
        "ticket": chat.ticket_id,
        "present": bool(chat.visitor_seen_at and chat.visitor_seen_at >= gone),
        "typing": _is_typing(chat.visitor_typing_at),
        "visitor": {
            "id": str(visitor.pk),
            "name": visitor.display_name,
            "email": visitor.email,
            "organization": chat.organization.name if chat.organization else "",
            "organization_id": str(chat.organization_id or ""),
            "workspace": chat.workspace.name if chat.workspace else "",
        },
        "device": {
            "browser": chat.browser,
            "system": chat.operating_system,
            "kind": chat.device,
            "screen": chat.screen,
            "language": chat.browser_language,
        },
        "page": {"path": chat.current_path, "title": short_title(chat.current_title)},
        "visits": [
            {"path": v.path, "title": short_title(v.title), "at": timezone.localtime(v.created_at).strftime("%H:%M")}
            for v in chat.visits.all()[:20]
        ],
        "messages": [_message_json(m) for m in chat.messages.filter(id__gt=after).select_related("author")],
    }


def pulse(agent) -> dict:
    """The two numbers the team's menu shows on every page: chats waiting and unread messages in the agent's chats."""
    queued = Chat.objects.filter(status=Chat.Status.QUEUED).count()
    unread = (
        ChatMessage.objects.filter(chat__agent=agent, chat__status=Chat.Status.ACTIVE, from_team=False)
        .filter(Q(chat__agent_seen_at__isnull=True) | Q(created_at__gt=F("chat__agent_seen_at")))
        .count()
    )
    return {"queued": queued, "unread": unread}


def _after_commit(callback) -> None:
    def run():
        try:
            callback()
        except Exception:
            logger.exception("A support e-mail could not be sent")

    transaction.on_commit(run)


def _emails():
    from . import emails

    return emails
