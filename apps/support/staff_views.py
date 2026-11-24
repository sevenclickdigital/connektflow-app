"""The team's side of support: the ticket queue, the frequent questions and the live chat console.

Every view is ``staff_required`` (``apps.accounts.decorators``): support and
superadmins alike answer customers. These paths carry no workspace or
organization; a ticket or chat is looked up by its id alone because the team
reads every one.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.decorators import staff_required

from . import services
from .models import Chat, FaqEntry, Ticket, TicketMessage
from .views import serve_attachment

FILTERS = ("active", "unassigned", "mine", "waiting_customer", "resolved", "all")


def _team():
    return get_user_model().objects.filter(is_staff=True, is_active=True).order_by("first_name", "email")


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------


@staff_required
@require_GET
def ticket_queue(request):
    current = request.GET.get("filter", "active")
    if current not in FILTERS:
        current = "active"
    query = " ".join(request.GET.get("q", "").split())[:100]
    kind = request.GET.get("kind", "")
    tickets = Ticket.objects.select_related("requester", "assignee", "organization").annotate(
        message_count=Count("messages", filter=Q(messages__is_internal=False)),
        last_customer_at=Max("messages__created_at", filter=Q(messages__from_team=False)),
        last_team_at=Max("messages__created_at", filter=Q(messages__from_team=True, messages__is_internal=False)),
    )
    if current == "active":
        tickets = tickets.filter(status__in=[Ticket.Status.OPEN, Ticket.Status.IN_PROGRESS])
    elif current == "unassigned":
        tickets = tickets.filter(assignee__isnull=True).exclude(status=Ticket.Status.RESOLVED)
    elif current == "mine":
        tickets = tickets.filter(assignee=request.user).exclude(status=Ticket.Status.RESOLVED)
    elif current in ("waiting_customer", "resolved"):
        tickets = tickets.filter(status=current)
    if kind in Ticket.Kind.values:
        tickets = tickets.filter(kind=kind)
    if query:
        lookup = Q(subject__icontains=query) | Q(requester__email__icontains=query)
        if query.lstrip("#").isdigit():
            lookup |= Q(pk=int(query.lstrip("#")))
        tickets = tickets.filter(lookup)
    tickets = list(tickets.order_by("-last_activity_at")[:200])
    for ticket in tickets:
        # Waiting on the team: the customer wrote last.
        ticket.awaits_team = bool(
            ticket.last_customer_at and (ticket.last_team_at is None or ticket.last_customer_at > ticket.last_team_at)
        )
    counts = {
        "active": Ticket.objects.filter(status__in=[Ticket.Status.OPEN, Ticket.Status.IN_PROGRESS]).count(),
        "unassigned": Ticket.objects.filter(assignee__isnull=True).exclude(status=Ticket.Status.RESOLVED).count(),
        "mine": Ticket.objects.filter(assignee=request.user).exclude(status=Ticket.Status.RESOLVED).count(),
    }
    return render(
        request,
        "support/staff_ticket_queue.html",
        {
            "admin_active": "support_tickets",
            "tickets": tickets,
            "current": current,
            "query": query,
            "kind": kind,
            "kinds": Ticket.Kind.choices,
            "counts": counts,
        },
    )


@staff_required
@require_GET
def ticket_detail(request, ticket_id):
    ticket = get_object_or_404(
        Ticket.objects.select_related("requester", "assignee", "organization", "workspace"), pk=ticket_id
    )
    thread = list(ticket.messages.select_related("author"))
    other_tickets = (
        Ticket.objects.filter(requester=ticket.requester).exclude(pk=ticket.pk).order_by("-last_activity_at")[:5]
    )
    chats = ticket.chats.all()
    return render(
        request,
        "support/staff_ticket_detail.html",
        {
            "admin_active": "support_tickets",
            "ticket": ticket,
            "thread": thread,
            "statuses": Ticket.Status.choices,
            "team": _team(),
            "other_tickets": other_tickets,
            "chats": chats,
        },
    )


@staff_required
@require_POST
def ticket_reply(request, ticket_id):
    ticket = get_object_or_404(Ticket, pk=ticket_id)
    internal = request.POST.get("internal") == "1"
    try:
        services.reply(
            ticket,
            author=request.user,
            body=request.POST.get("body", ""),
            from_team=True,
            internal=internal,
            attachment=request.FILES.get("attachment"),
        )
    except services.SupportError as exc:
        messages.error(request, str(exc))
    else:
        status = request.POST.get("status", "")
        if status in Ticket.Status.values and status != ticket.status:
            services.set_status(ticket, status)
        messages.success(request, _("Note saved.") if internal else _("Reply sent to the customer."))
    return redirect("support_staff:ticket", ticket_id=ticket.pk)


@staff_required
@require_POST
def ticket_update(request, ticket_id):
    ticket = get_object_or_404(Ticket, pk=ticket_id)
    status = request.POST.get("status")
    if status is not None:
        try:
            services.set_status(ticket, status)
        except services.SupportError as exc:
            messages.error(request, str(exc))
    if "assignee" in request.POST:
        assignee_id = request.POST.get("assignee") or None
        assignee = get_object_or_404(_team(), pk=assignee_id) if assignee_id else None
        services.assign(ticket, assignee)
    messages.success(request, _("Ticket updated."))
    return redirect("support_staff:ticket", ticket_id=ticket.pk)


@staff_required
@require_GET
def ticket_attachment(request, ticket_id, message_id):
    message = get_object_or_404(TicketMessage, pk=message_id, ticket_id=ticket_id)
    return serve_attachment(message)


# ---------------------------------------------------------------------------
# Frequent questions
# ---------------------------------------------------------------------------


@staff_required
@require_GET
def faq_list(request):
    entries = FaqEntry.objects.all()
    topics: dict[str, list] = {}
    for entry in entries:
        topics.setdefault(entry.topic, []).append(entry)
    return render(request, "support/staff_faq_list.html", {"admin_active": "support_faq", "topics": topics})


@staff_required
@require_http_methods(["GET", "POST"])
def faq_edit(request, entry_id=None):
    entry = get_object_or_404(FaqEntry, pk=entry_id) if entry_id else FaqEntry()
    errors = {}
    if request.method == "POST":
        entry.topic = " ".join(request.POST.get("topic", "").split())[:80]
        entry.question = " ".join(request.POST.get("question", "").split())[:200]
        entry.answer_markdown = request.POST.get("answer_markdown", "").strip()
        entry.is_published = request.POST.get("is_published") == "1"
        try:
            entry.position = max(int(request.POST.get("position", "0") or 0), 0)
        except ValueError:
            entry.position = 0
        for name in ("topic", "question", "answer_markdown"):
            if not getattr(entry, name):
                errors[name] = _("Fill this in.")
        if not errors:
            entry.save()
            messages.success(request, _("Question saved."))
            return redirect("support_staff:faq")
    existing_topics = FaqEntry.objects.order_by("topic").values_list("topic", flat=True).distinct()
    return render(
        request,
        "support/staff_faq_edit.html",
        {"admin_active": "support_faq", "entry": entry, "errors": errors, "existing_topics": existing_topics},
    )


@staff_required
@require_POST
def faq_delete(request, entry_id):
    get_object_or_404(FaqEntry, pk=entry_id).delete()
    messages.success(request, _("Question removed."))
    return redirect("support_staff:faq")


# ---------------------------------------------------------------------------
# Live chat console (JSON, polled every few seconds by the console page)
# ---------------------------------------------------------------------------


def _after(request) -> int:
    try:
        return max(int(request.GET.get("after", 0)), 0)
    except ValueError:
        return 0


@staff_required
@require_GET
def chat_console(request):
    services.mark_presence(request.user)
    return render(
        request,
        "support/staff_chat_console.html",
        {"admin_active": "support_chat", "kinds": Ticket.Kind.choices},
    )


HISTORY_FILTERS = ("all", "mine", "ticket")


@staff_required
@require_GET
def chat_history(request):
    """Every chat, newest first: by customer (name or e-mail), the agent's own, the ones that became tickets."""
    current = request.GET.get("filter", "all")
    if current not in HISTORY_FILTERS:
        current = "all"
    query = " ".join(request.GET.get("q", "").split())[:100]
    chats = services.with_question(Chat.objects.select_related("visitor", "agent", "organization", "ticket"))
    if current == "mine":
        chats = chats.filter(agent=request.user)
    elif current == "ticket":
        chats = chats.filter(ticket__isnull=False)
    if query:
        chats = chats.filter(
            Q(visitor__email__icontains=query)
            | Q(visitor__first_name__icontains=query)
            | Q(visitor__last_name__icontains=query)
            | Q(visitor__name__icontains=query)
        )
    chats = chats.order_by("-created_at")[:200]
    return render(
        request,
        "support/staff_chat_history.html",
        {"admin_active": "support_chat", "chats": chats, "current": current, "query": query},
    )


@staff_required
@require_GET
def chat_history_detail(request, chat_id):
    """One chat for the team: the messages, the visitor, their device and the pages they went through."""
    chat = get_object_or_404(
        Chat.objects.select_related("visitor", "agent", "organization", "workspace", "ticket"), pk=chat_id
    )
    thread = list(chat.messages.select_related("author"))
    visits = list(chat.visits.all()[:50])
    for visit in visits:
        visit.short_title = services.short_title(visit.title)
    return render(
        request,
        "support/staff_chat_history_detail.html",
        {"admin_active": "support_chat", "chat": chat, "thread": thread, "visits": visits},
    )


@staff_required
@require_GET
def chat_feed(request):
    services.mark_presence(request.user)
    return JsonResponse(services.console_feed(request.user))


@staff_required
@require_GET
def chat_pulse(request):
    return JsonResponse(services.pulse(request.user))


@staff_required
@require_GET
def chat_detail(request, chat_id):
    chat = get_object_or_404(Chat.objects.select_related("visitor", "agent", "organization", "workspace"), pk=chat_id)
    return JsonResponse(services.console_chat(chat, request.user, after=_after(request)))


@staff_required
@require_POST
def chat_accept(request, chat_id):
    chat = services.accept(chat_id, request.user)
    if chat is None:
        return JsonResponse({"error": _("Another agent took this chat, or the visitor left.")}, status=409)
    return JsonResponse({"ok": True})


@staff_required
@require_POST
def chat_send(request, chat_id):
    chat = get_object_or_404(Chat, pk=chat_id)
    try:
        services.agent_writes(chat, request.user, request.POST.get("body", ""))
    except services.SupportError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True})


@staff_required
@require_POST
def chat_close(request, chat_id):
    chat = get_object_or_404(Chat, pk=chat_id)
    services.agent_closes(chat)
    return JsonResponse({"ok": True})


@staff_required
@require_POST
def chat_convert(request, chat_id):
    chat = get_object_or_404(Chat, pk=chat_id)
    if chat.status == Chat.Status.CLOSED:
        return JsonResponse({"error": _("This chat is already closed.")}, status=409)
    ticket = services.convert_to_ticket(
        chat,
        agent=request.user,
        subject=request.POST.get("subject", ""),
        kind=request.POST.get("kind", Ticket.Kind.SUPPORT),
        note=request.POST.get("note", ""),
    )
    return JsonResponse({"ok": True, "ticket": ticket.pk, "url": reverse("support_staff:ticket", args=[ticket.pk])})
