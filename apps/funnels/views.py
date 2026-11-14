"""Funnel screens: the inbox seen through a funnel, the board, and the funnels' settings."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import pgettext
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response
from apps.contacts.services import clean_tags
from apps.inbox.models import InboxMessage
from apps.members.decorators import require_permission

from . import services
from .models import Funnel, FunnelStage


def _workspace(request, workspace_id):
    workspace = request.workspace
    if workspace is None or str(workspace.id) != str(workspace_id):
        raise PermissionDenied
    return workspace


def _pick_funnel(funnels: list[Funnel], funnel_id: str | None) -> Funnel:
    if funnel_id:
        for funnel in funnels:
            if str(funnel.id) == funnel_id:
                return funnel
    return next((f for f in funnels if f.is_default), funnels[0])


def _decorate_rows(messages, stages_by_id: dict) -> None:
    for message in messages:
        message.stage = stages_by_id.get(str(getattr(message, "funnel_stage", "") or ""))


# ---------------------------------------------------------------------------
# The inbox, through one funnel
# ---------------------------------------------------------------------------


@login_required
@require_permission("use_inbox")
@require_GET
def funnel_inbox(request, workspace_id):
    """The inbox's conversations of one funnel, filtered by stage, with their opportunity."""
    from apps.composer.emoji import emoji_catalogue
    from apps.inbox.views import feed_context

    workspace = _workspace(request, workspace_id)
    funnels = services.funnels_for(workspace)
    funnel = _pick_funnel(funnels, request.GET.get("funnel"))
    stages = list(funnel.stages.all())
    stages_by_id = {str(stage.id): stage for stage in stages}
    active_stage = request.GET.get("stage", "")
    if active_stage not in stages_by_id:
        active_stage = ""

    def scope(threads):
        threads = services.with_stage(threads, funnel)
        counts = services.stage_counts(threads, funnel)
        if active_stage:
            threads = threads.filter(funnel_stage=active_stage)
        return threads, {
            "funnel": funnel,
            "funnels": funnels,
            "stages": [(stage, counts.get(str(stage.id), 0)) for stage in stages],
            "active_stage": active_stage,
        }

    context = feed_context(request, workspace, scope_threads=scope)
    _decorate_rows(context["inbox_messages"], stages_by_id)
    context["list_url"] = reverse("funnels:inbox", kwargs={"workspace_id": workspace.id})
    context["detail_query"] = "?mode=funnel"
    if request.htmx:
        return render(request, "inbox/partials/_message_list.html", context)
    context["emoji_categories"] = emoji_catalogue()
    return render(request, "inbox/feed.html", context)


def opportunity_context(workspace, message) -> dict:
    """What the entry's "Opportunity details" panel shows (inbox message_detail, mode=funnel)."""
    from apps.automations.services import flows_for_message
    from apps.contacts.services import contact_for_message

    funnels = services.funnels_for(workspace)
    funnel, stage, opportunity = services.place_of(message, funnels)
    return {
        "panel_mode": "funnel",
        "funnels": funnels,
        "opp_funnel": funnel,
        "opp_stage": stage,
        "opportunity": opportunity,
        "next_stage": services.next_stage(stage) if stage else None,
        "interest_suggestions": services.interest_suggestions(workspace.id),
        "contact": contact_for_message(message),
        "flows": flows_for_message(message),
    }


def _panel(request, workspace, message):
    from apps.inbox.views import _detail_context

    context = _detail_context(workspace, message, actor=request.user)
    context.update(opportunity_context(workspace, message))
    response = render(request, "inbox/partials/_message_panel.html", context)
    # The list shows the stage and the bar counts it: refresh both.
    response["HX-Trigger"] = "inbox-list-refresh"
    return response


def _message(workspace, message_id) -> InboxMessage:
    return get_object_or_404(
        InboxMessage.objects.select_related("social_account", "assigned_to", "related_post__post"),
        id=message_id,
        workspace=workspace,
    )


@login_required
@require_permission("reply_from_inbox")
@require_POST
def opportunity_update(request, workspace_id, message_id):
    """Save one field of the conversation's opportunity: stage, value, interest, tags or notes."""
    workspace = _workspace(request, workspace_id)
    message = _message(workspace, message_id)
    funnels = services.funnels_for(workspace)
    field = request.POST.get("field", "")

    if field == "funnel":
        target = get_object_or_404(Funnel, id=request.POST.get("value"), workspace=workspace)
        entry = services.entry_stage(target)
        if entry is None:
            return toast_response(tone="error", title=_("That funnel has no stages yet."))
        services.move(services.ensure_opportunity(message, funnels), entry, actor=request.user)
    elif field == "stage":
        stage = get_object_or_404(FunnelStage, id=request.POST.get("value"), funnel__workspace=workspace)
        services.move(services.ensure_opportunity(message, funnels), stage, actor=request.user)
    elif field == "value":
        value = services.parse_value(request.POST.get("value", ""))
        if value is None:
            return toast_response(tone="error", title=_("Type the value as a number, e.g. 1.500,00."))
        opportunity = services.ensure_opportunity(message, funnels)
        opportunity.value = value
        opportunity.save(update_fields=["value", "updated_at"])
    elif field in ("interest", "notes"):
        limit = 100 if field == "interest" else 5000
        opportunity = services.ensure_opportunity(message, funnels)
        setattr(opportunity, field, request.POST.get("value", "").strip()[:limit])
        opportunity.save(update_fields=[field, "updated_at"])
    elif field in ("add_tag", "remove_tag"):
        opportunity = services.ensure_opportunity(message, funnels)
        tag = request.POST.get("value", "").strip()
        tags = list(opportunity.tags or [])
        if field == "add_tag":
            tags.append(tag)
        else:
            tags = [t for t in tags if t != tag]
        opportunity.tags = clean_tags(tags)
        opportunity.save(update_fields=["tags", "updated_at"])
    else:
        raise Http404
    return _panel(request, workspace, message)


@login_required
@require_permission("reply_from_inbox")
@require_POST
def opportunity_next(request, workspace_id, message_id):
    """Move the conversation to the next stage of its funnel."""
    workspace = _workspace(request, workspace_id)
    message = _message(workspace, message_id)
    opportunity = services.ensure_opportunity(message, services.funnels_for(workspace))
    target = services.next_stage(opportunity.stage)
    if target is None:
        return toast_response(tone="info", title=_("This is already the last stage of the funnel."))
    services.move(opportunity, target, actor=request.user)
    return _panel(request, workspace, message)


@login_required
@require_permission("reply_from_inbox")
@require_POST
def flow_start(request, workspace_id, message_id):
    """Run the post's flow for this conversation's comment (the composer's Automation tab)."""
    from apps.automations.actions import start_flow_for_message
    from apps.automations.services import flows_for_message

    workspace = _workspace(request, workspace_id)
    message = _message(workspace, message_id)
    flow = next((f for f in flows_for_message(message) if str(f.id) == request.POST.get("flow")), None)
    if flow is None:
        raise Http404
    sent, reason = start_flow_for_message(message, flow.rule)
    if not sent:
        return toast_response(tone="error", title=_("The flow was not started."), body=reason)
    return toast_response(tone="success", title=_("Flow started for this person."))


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

PERIODS = (7, 30, 90)


@login_required
@require_permission("use_inbox")
@require_GET
def board(request, workspace_id):
    """The whole funnel in columns: one per stage, with figures, filters and recent activity."""
    from apps.members.models import WorkspaceMembership
    from apps.social_accounts.models import SocialAccount

    workspace = _workspace(request, workspace_id)
    funnels = services.funnels_for(workspace)
    funnel = _pick_funnel(funnels, request.GET.get("funnel"))
    try:
        days = int(request.GET.get("days", "30"))
    except ValueError:
        days = 30
    days = days if days in PERIODS else 0
    filters = {
        "platform": request.GET.get("platform", ""),
        "assigned": request.GET.get("assigned", ""),
        "tag": request.GET.get("tag", ""),
        "origin": request.GET.get("origin", ""),
        "q": request.GET.get("q", "").strip(),
    }
    data = services.board(workspace, funnel, days=days or None, **filters)
    context = {
        "workspace": workspace,
        "funnels": funnels,
        "funnel": funnel,
        "days": days,
        "periods": PERIODS,
        "filters": filters,
        "active_filters": {"platforms": [filters["platform"]] if filters["platform"] else []},
        "social_accounts": SocialAccount.objects.filter(
            workspace=workspace, connection_status=SocialAccount.ConnectionStatus.CONNECTED
        ).order_by("platform"),
        "team_members": WorkspaceMembership.objects.filter(workspace=workspace).select_related("user"),
        **data,
    }
    if request.htmx and request.GET.get("pick") is not None:
        context["candidates"] = services.candidates(workspace, funnel, request.GET.get("pick", "").strip())
        requested = request.GET.get("stage", "")
        context["pick_stage"] = next(
            (str(c["stage"].id) for c in data["columns"] if str(c["stage"].id) == requested), ""
        )
        return render(request, "funnels/partials/_pick_conversation.html", context)
    return render(request, "funnels/board.html", context)


@login_required
@require_permission("reply_from_inbox")
@require_POST
def board_move(request, workspace_id):
    """Put a conversation in a stage: a card dropped on a column, or one added to it."""
    workspace = _workspace(request, workspace_id)
    message = _message(workspace, request.POST.get("message"))
    stage = get_object_or_404(FunnelStage, id=request.POST.get("stage"), funnel__workspace=workspace)
    opportunity = services.ensure_opportunity(message, services.funnels_for(workspace))
    services.move(opportunity, stage, actor=request.user)
    return toast_response(
        tone="success",
        title=_("Moved to %(stage)s.") % {"stage": stage.name},
        events={"board-refresh": True},
    )


# ---------------------------------------------------------------------------
# Settings: funnels and their stages
# ---------------------------------------------------------------------------


def _settings_response(request, workspace, *, tone="", title=""):
    context = {
        "workspace": workspace,
        "funnels": services.funnels_for(workspace),
        "stage_colors": FunnelStage.Color.choices,
        "stage_kinds": FunnelStage.Kind.choices,
    }
    response = render(request, "funnels/partials/_settings_list.html", context)
    if title:
        response["HX-Trigger"] = toast_response(tone=tone, title=title)["HX-Trigger"]
    return response


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def funnel_settings(request, workspace_id):
    workspace = _workspace(request, workspace_id)
    context = {
        "workspace": workspace,
        "funnels": services.funnels_for(workspace),
        "stage_colors": FunnelStage.Color.choices,
        "stage_kinds": FunnelStage.Kind.choices,
    }
    return render(request, "funnels/settings.html", context)


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def funnel_save(request, workspace_id):
    """Create a funnel, or rename one (``funnel`` in the form)."""
    workspace = _workspace(request, workspace_id)
    name = request.POST.get("name", "").strip()[:80]
    if not name:
        return toast_response(tone="error", title=_("Give the funnel a name."))
    funnel_id = request.POST.get("funnel")
    if funnel_id:
        funnel = get_object_or_404(Funnel, id=funnel_id, workspace=workspace)
        funnel.name = name
        funnel.save(update_fields=["name"])
        return _settings_response(request, workspace, tone="success", title=_("Funnel renamed."))
    funnels = services.funnels_for(workspace)
    funnel = Funnel.objects.create(workspace=workspace, name=name, position=len(funnels))
    FunnelStage.objects.create(
        funnel=funnel, name=pgettext("funnel stage", "New lead"), color=FunnelStage.Color.VIOLET, position=0
    )
    return _settings_response(request, workspace, tone="success", title=_("Funnel created."))


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def funnel_delete(request, workspace_id, funnel_id):
    workspace = _workspace(request, workspace_id)
    funnel = get_object_or_404(Funnel, id=funnel_id, workspace=workspace)
    if funnel.is_default:
        return toast_response(tone="error", title=_("The main funnel cannot be deleted."))
    # Its conversations go back to the main funnel's first stage.
    funnel.delete()
    return _settings_response(request, workspace, tone="success", title=_("Funnel deleted."))


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def stage_save(request, workspace_id, funnel_id):
    """Add a stage to a funnel, or edit one (``stage`` in the form)."""
    workspace = _workspace(request, workspace_id)
    funnel = get_object_or_404(Funnel, id=funnel_id, workspace=workspace)
    name = request.POST.get("name", "").strip()[:60]
    if not name:
        return toast_response(tone="error", title=_("Give the stage a name."))
    color = request.POST.get("color", "")
    kind = request.POST.get("kind", "")
    color = color if color in FunnelStage.Color.values else FunnelStage.Color.VIOLET
    kind = kind if kind in FunnelStage.Kind.values else FunnelStage.Kind.OPEN
    stage_id = request.POST.get("stage")
    if stage_id:
        stage = get_object_or_404(FunnelStage, id=stage_id, funnel=funnel)
        stage.name, stage.color, stage.kind = name, color, kind
        stage.save(update_fields=["name", "color", "kind"])
        return _settings_response(request, workspace, tone="success", title=_("Stage saved."))
    FunnelStage.objects.create(funnel=funnel, name=name, color=color, kind=kind, position=funnel.stages.count())
    return _settings_response(request, workspace, tone="success", title=_("Stage added."))


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def stage_delete(request, workspace_id, stage_id):
    workspace = _workspace(request, workspace_id)
    stage = get_object_or_404(FunnelStage, id=stage_id, funnel__workspace=workspace)
    try:
        target = services.delete_stage(stage)
    except ValueError as exc:
        return toast_response(tone="error", title=str(exc))
    return _settings_response(
        request,
        workspace,
        tone="success",
        title=_("Stage deleted. Its conversations moved to %(stage)s.") % {"stage": target.name},
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def stage_reorder(request, workspace_id, stage_id):
    workspace = _workspace(request, workspace_id)
    stage = get_object_or_404(FunnelStage, id=stage_id, funnel__workspace=workspace)
    services.reorder_stage(stage, -1 if request.POST.get("direction") == "up" else 1)
    return _settings_response(request, workspace)
