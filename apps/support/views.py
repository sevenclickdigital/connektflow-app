"""The customer's side of support: the help centre, their tickets and the chat widget's calls.

Every view is ``login_required`` and reaches only the requester's own data:
tickets are looked up with ``requester=request.user``, the chat is the
user's open one (``services.open_chat_of``), never an id from the request.
A team account never gets here (``OperatorAreaMiddleware`` sends it back to
the administrative area), and an operator inside a customer's account may
read but not write (``no_takeover``): a ticket or a chat message would go
out in the customer's name.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.impersonation import no_takeover
from apps.onboarding import help_videos
from apps.onboarding.models import PageHelpVideo

from . import services
from .models import Chat, FaqEntry, Ticket, TicketMessage


def _context(request):
    """Where the person is in the product, kept on what they open for the team."""
    return {"organization": getattr(request, "org", None), "workspace": getattr(request, "workspace", None)}


@login_required
@require_GET
def help_center(request):
    query = " ".join(request.GET.get("q", "").split())[:100]
    entries = FaqEntry.objects.filter(is_published=True)
    if query:
        entries = entries.filter(Q(question__icontains=query) | Q(answer_markdown__icontains=query))
    topics: dict[str, list] = {}
    for entry in entries:
        topics.setdefault(entry.topic, []).append(entry)
    # The tutorials have their own pages; here they only show as search results.
    tutorials = []
    if query:
        lowered = query.lower()
        tutorials = [
            t for t in help_videos.tutorials() if lowered in str(t["title"]).lower() or lowered in t["text"].lower()
        ]
    open_tickets = Ticket.objects.filter(requester=request.user).exclude(status=Ticket.Status.RESOLVED).count()
    return render(
        request,
        "support/help_center.html",
        {
            "query": query,
            "topics": topics,
            "tutorials": tutorials,
            "open_tickets": open_tickets,
        },
    )


@login_required
@require_GET
def tutorial_list(request, kind):
    """The quick video tutorials (``kind="videos"``) or the user guide, the tutorials with a text (``"guides"``)."""
    tutorials = help_videos.tutorials()
    items = [t for t in tutorials if (t["video_id"] if kind == "videos" else t["text"])]
    if kind == "videos":
        title, text = (
            _("Quick tutorials"),
            _("Short videos showing, step by step, how to use each part of the platform."),
        )
    else:
        title, text = _("User guide"), _("Step-by-step guides to the platform's main features.")
    return render(
        request,
        "support/tutorial_list.html",
        {"kind": kind, "items": items, "page_title": title, "page_text": text},
    )


@login_required
@require_GET
def tutorial(request, page):
    found = next((t for t in help_videos.tutorials() if t["page"] == page), None)
    if found is None or page not in PageHelpVideo.Page.values:
        raise Http404("No tutorial for this page.")
    return render(request, "support/tutorial.html", {"tutorial": found})


@login_required
@require_GET
def ticket_list(request):
    tickets = list(services.tickets_of(request.user))
    for ticket in tickets:
        ticket.has_news = services.has_news(ticket)
    return render(request, "support/ticket_list.html", {"tickets": tickets})


@login_required
@no_takeover
def ticket_new(request):
    form = {"kind": request.GET.get("kind", Ticket.Kind.SUPPORT), "subject": "", "body": ""}
    if request.method == "POST":
        form = {key: request.POST.get(key, "") for key in ("kind", "subject", "body")}
        try:
            ticket = services.open_ticket(
                requester=request.user,
                kind=form["kind"],
                subject=form["subject"],
                body=form["body"],
                attachment=request.FILES.get("attachment"),
                **_context(request),
            )
        except services.SupportError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request, _("Ticket #%(number)s opened. We'll answer here and by e-mail.") % {"number": ticket.pk}
            )
            return redirect("support:ticket", ticket_id=ticket.pk)
    if form["kind"] not in Ticket.Kind.values:
        form["kind"] = Ticket.Kind.SUPPORT
    return render(request, "support/ticket_new.html", {"form": form, "kinds": Ticket.Kind.choices})


@login_required
@require_GET
def ticket_detail(request, ticket_id):
    ticket = get_object_or_404(Ticket, pk=ticket_id, requester=request.user)
    thread = list(services.customer_messages(ticket))
    services.mark_seen_by_requester(ticket)
    return render(request, "support/ticket_detail.html", {"ticket": ticket, "thread": thread})


@login_required
@no_takeover
@require_POST
def ticket_reply(request, ticket_id):
    ticket = get_object_or_404(Ticket, pk=ticket_id, requester=request.user)
    try:
        services.reply(
            ticket,
            author=request.user,
            body=request.POST.get("body", ""),
            from_team=False,
            attachment=request.FILES.get("attachment"),
        )
    except services.SupportError as exc:
        messages.error(request, str(exc))
    return redirect("support:ticket", ticket_id=ticket.pk)


@login_required
@require_GET
def ticket_attachment(request, ticket_id, message_id):
    message = get_object_or_404(
        TicketMessage, pk=message_id, ticket_id=ticket_id, ticket__requester=request.user, is_internal=False
    )
    return serve_attachment(message)


def serve_attachment(message: TicketMessage):
    """The message's image, private: support_attachments/ is not among the public media prefixes."""
    if not message.attachment:
        raise Http404("This message has no attachment.")
    try:
        handle = message.attachment.open("rb")
    except Exception:  # noqa: BLE001 - storage backends raise varied errors (OSError, botocore ClientError)
        raise Http404("Attachment is no longer stored.") from None
    response = FileResponse(handle)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=3600"
    return response


@login_required
@require_GET
def chat_history(request):
    """The person's chats with the team, newest first."""
    chats = services.chats_of(request.user)[:100]
    return render(request, "support/chat_history.html", {"chats": chats})


@login_required
@require_GET
def chat_history_detail(request, chat_id):
    """One of the person's chats: the messages only, nothing of what the team sees about the visit."""
    chat = get_object_or_404(Chat.objects.select_related("agent", "ticket"), pk=chat_id, visitor=request.user)
    thread = list(chat.messages.select_related("author"))
    return render(request, "support/chat_history_detail.html", {"chat": chat, "thread": thread})


# ---------------------------------------------------------------------------
# The chat widget (JSON, polled every few seconds while a chat is open)
# ---------------------------------------------------------------------------


def _after(request) -> int:
    try:
        return max(int(request.GET.get("after", 0)), 0)
    except ValueError:
        return 0


@login_required
@require_GET
def chat_state(request):
    return JsonResponse(services.visitor_state(request.user, after=_after(request)))


@login_required
@no_takeover
@require_POST
def chat_send(request):
    page = {key: request.POST.get(key, "") for key in ("path", "title", "screen", "language")}
    try:
        result = services.visitor_writes(
            request.user,
            request.POST.get("body", ""),
            user_agent=request.headers.get("User-Agent", ""),
            page=page,
            **_context(request),
        )
    except services.SupportError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    state = services.visitor_state(request.user, after=_after(request))
    if isinstance(result, Ticket):
        state["ticket"] = {"number": result.pk, "url": reverse("support:ticket", kwargs={"ticket_id": result.pk})}
    return JsonResponse(state)


@login_required
@no_takeover
@require_POST
def chat_visit(request):
    chat = services.open_chat_of(request.user)
    if chat is not None:
        services.record_visit(chat, request.POST.get("path", ""), request.POST.get("title", ""))
    return JsonResponse({"ok": True})


@login_required
@no_takeover
@require_POST
def chat_close(request):
    chat = services.open_chat_of(request.user)
    if chat is not None and chat.status != Chat.Status.CLOSED:
        services.visitor_closes(chat)
    return JsonResponse({"ok": True})
