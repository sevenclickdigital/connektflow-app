"""Funnels over inbox conversations: the default funnel, stages, moves and the board."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Case, CharField, Count, F, OuterRef, Q, Subquery, Value, When
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import pgettext_lazy

from apps.inbox import services as inbox_services
from apps.inbox.models import InboxMessage

from .models import Funnel, FunnelStage, Opportunity, OpportunityActivity

# The funnel every workspace starts with, created on first use.
# Names are written in the language of whoever opens the funnel first.
DEFAULT_STAGES = (
    (pgettext_lazy("funnel stage", "New lead"), FunnelStage.Color.VIOLET, FunnelStage.Kind.OPEN),
    (pgettext_lazy("funnel stage", "Qualification"), FunnelStage.Color.BLUE, FunnelStage.Kind.OPEN),
    (pgettext_lazy("funnel stage", "Proposal"), FunnelStage.Color.AMBER, FunnelStage.Kind.OPEN),
    (pgettext_lazy("funnel stage", "Negotiation"), FunnelStage.Color.PURPLE, FunnelStage.Kind.OPEN),
    (pgettext_lazy("funnel stage", "Converted"), FunnelStage.Color.GREEN, FunnelStage.Kind.WON),
)


def funnels_for(workspace) -> list[Funnel]:
    """The workspace's funnels, the default first created when it has none."""
    funnels = list(Funnel.objects.for_workspace(workspace.id).prefetch_related("stages"))
    if funnels:
        return funnels
    with transaction.atomic():
        funnel, created = Funnel.objects.get_or_create(
            workspace=workspace, is_default=True, defaults={"name": _("Sales - Main")}
        )
        if created:
            FunnelStage.objects.bulk_create(
                FunnelStage(funnel=funnel, name=str(name), color=color, kind=kind, position=index)
                for index, (name, color, kind) in enumerate(DEFAULT_STAGES)
            )
    return list(Funnel.objects.for_workspace(workspace.id).prefetch_related("stages"))


def entry_stage(funnel: Funnel) -> FunnelStage | None:
    """Where a conversation lands in ``funnel``: its first stage."""
    stages = list(funnel.stages.all())
    return stages[0] if stages else None


# ---------------------------------------------------------------------------
# Conversations and their opportunity
# ---------------------------------------------------------------------------


def _key_filter(prefix: str = "") -> dict:
    """Opportunity fields matching an inbox row annotated by ``with_thread_key``."""
    return {
        "social_account_id": OuterRef(f"{prefix}social_account_id"),
        "message_type": OuterRef(f"{prefix}message_type"),
        "thread_sender": OuterRef(f"{prefix}thread_sender"),
        "thread_post": OuterRef(f"{prefix}thread_post"),
    }


def with_stage(qs, funnel: Funnel):
    """Annotate inbox rows (keyed by ``with_thread_key``) with their place in ``funnel``.

    ``opp_funnel`` is the funnel the conversation's opportunity is in (None when it
    has none); ``funnel_stage`` is the stage it sits in for ``funnel``: its
    opportunity's, or the entry stage when it has none and ``funnel`` is the
    default. Rows outside ``funnel`` get no stage.
    """
    opportunities = Opportunity.objects.filter(workspace_id=OuterRef("workspace_id"), **_key_filter())
    entry = entry_stage(funnel)
    qs = qs.annotate(
        opp_funnel=Subquery(opportunities.values("funnel_id")[:1]),
        opp_stage=Subquery(opportunities.values("stage_id")[:1]),
        opportunity_id=Subquery(opportunities.values("id")[:1]),
        opp_value=Subquery(opportunities.values("value")[:1]),
        opp_tags=Subquery(opportunities.values("tags")[:1]),
    )
    in_funnel = Q(opp_funnel=funnel.id)
    if funnel.is_default and entry:
        in_funnel |= Q(opp_funnel__isnull=True)
    return qs.filter(in_funnel).annotate(
        funnel_stage=Case(
            When(opp_funnel=funnel.id, then=F("opp_stage")),
            default=Value(str(entry.id) if entry else None),
            output_field=CharField(),
        )
    )


def thread_opportunity(message: InboxMessage) -> Opportunity | None:
    """The opportunity of ``message``'s conversation, if one was written."""
    account_id, message_type, sender, post = inbox_services.thread_key(message)
    return (
        Opportunity.objects.filter(
            workspace_id=message.workspace_id,
            social_account_id=account_id,
            message_type=message_type,
            thread_sender=sender,
            thread_post=post,
        )
        .select_related("funnel", "stage")
        .first()
    )


def place_of(
    message: InboxMessage, funnels: list[Funnel]
) -> tuple[Funnel | None, FunnelStage | None, Opportunity | None]:
    """Where ``message``'s conversation sits: (funnel, stage, opportunity)."""
    opportunity = thread_opportunity(message)
    if opportunity:
        return opportunity.funnel, opportunity.stage, opportunity
    default = next((f for f in funnels if f.is_default), funnels[0] if funnels else None)
    return default, (entry_stage(default) if default else None), None


def ensure_opportunity(message: InboxMessage, funnels: list[Funnel]) -> Opportunity:
    """The conversation's opportunity, written in its current place when it had none."""
    funnel, stage, opportunity = place_of(message, funnels)
    if opportunity:
        return opportunity
    if funnel is None or stage is None:
        raise ValueError(_("This workspace has no funnel stage to place the conversation in."))
    account_id, message_type, sender, post = inbox_services.thread_key(message)
    opportunity, _created = Opportunity.objects.get_or_create(
        workspace_id=message.workspace_id,
        social_account_id=account_id,
        message_type=message_type,
        thread_sender=sender,
        thread_post=post,
        defaults={"funnel": funnel, "stage": stage, "name": message.sender_name[:255]},
    )
    return opportunity


def move(opportunity: Opportunity, stage: FunnelStage, *, actor=None) -> Opportunity:
    """Put ``opportunity`` in ``stage`` (of any funnel of its workspace) and record it."""
    if stage.funnel.workspace_id != opportunity.workspace_id:
        raise ValueError("Stage of another workspace.")
    if opportunity.stage_id == stage.id:
        return opportunity
    opportunity.funnel_id = stage.funnel_id
    opportunity.stage = stage
    opportunity.stage_changed_at = timezone.now()
    opportunity.save(update_fields=["funnel", "stage", "stage_changed_at", "updated_at"])
    OpportunityActivity.objects.create(
        workspace_id=opportunity.workspace_id,
        opportunity=opportunity,
        kind=OpportunityActivity.Kind.MOVED,
        stage=stage,
        actor=actor,
    )
    return opportunity


def next_stage(stage: FunnelStage) -> FunnelStage | None:
    stages = list(stage.funnel.stages.all())
    index = next((i for i, s in enumerate(stages) if s.id == stage.id), None)
    if index is None or index + 1 >= len(stages):
        return None
    return stages[index + 1]


def parse_value(raw: str) -> Decimal | None:
    """A money amount as typed in Brazil ("1.234,56", "1234.56", "R$ 90"), or None."""
    text = (raw or "").replace("R$", "").replace(" ", "").strip()
    if not text:
        return Decimal("0")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value < 0 or value >= Decimal("10000000000"):
        return None
    return value.quantize(Decimal("0.01"))


def stage_counts(threads, funnel: Funnel) -> dict[str, int]:
    """Conversations per stage of ``funnel`` among ``threads`` (rows annotated by ``with_stage``)."""
    return {
        str(row["funnel_stage"]): row["n"] for row in threads.order_by().values("funnel_stage").annotate(n=Count("id"))
    }


def interest_suggestions(workspace_id) -> list[str]:
    values = (
        Opportunity.objects.for_workspace(workspace_id)
        .exclude(interest="")
        .values_list("interest", flat=True)
        .distinct()[:50]
    )
    return sorted(set(values), key=str.lower)


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

BOARD_CARDS_PER_STAGE = 50
ORIGIN_FORMATS = {origin: fmt for fmt, origin in InboxMessage.FORMAT_ORIGINS.items()}


def board(workspace, funnel: Funnel, *, platform="", assigned="", tag="", origin="", q="", days=None):
    """The board of ``funnel``: its stages with their conversations, totals and the funnel's figures."""
    from datetime import timedelta

    from django.db.models import Sum

    qs = InboxMessage.objects.for_workspace(workspace.id)
    if platform:
        qs = qs.filter(social_account__platform=platform)
    if assigned == "none":
        qs = qs.filter(assigned_to__isnull=True)
    elif assigned:
        qs = qs.filter(assigned_to_id=assigned)
    if origin in ORIGIN_FORMATS:
        qs = qs.filter(Q(extra__post_origin=origin) | Q(related_post__format=ORIGIN_FORMATS[origin]))
    if q:
        qs = qs.filter(Q(body__icontains=q) | Q(sender_name__icontains=q) | Q(sender_handle__icontains=q))
    if days:
        qs = qs.filter(received_at__gte=timezone.now() - timedelta(days=days))

    threads = with_stage(inbox_services.latest_per_thread(qs), funnel)
    if tag:
        threads = threads.filter(opp_tags__contains=[tag])
    stages = list(funnel.stages.all())
    totals = {
        str(row["funnel_stage"]): row
        for row in threads.order_by().values("funnel_stage").annotate(n=Count("id"), value=Sum("opp_value"))
    }
    columns = []
    for stage in stages:
        cards = list(
            threads.filter(funnel_stage=str(stage.id))
            .select_related("social_account")
            .order_by("-received_at")[:BOARD_CARDS_PER_STAGE]
        )
        row = totals.get(str(stage.id), {})
        columns.append({"stage": stage, "cards": cards, "count": row.get("n", 0), "value": row.get("value") or 0})

    total = sum(column["count"] for column in columns)
    won = sum(c["count"] for c in columns if c["stage"].kind == FunnelStage.Kind.WON)
    open_count = sum(c["count"] for c in columns if c["stage"].kind == FunnelStage.Kind.OPEN)
    for column in columns:
        column["share"] = round(column["count"] * 100 / total) if total else 0
    return {
        "columns": columns,
        "figures": {
            "total": total,
            "open": open_count,
            "won": won,
            "value": sum(c["value"] for c in columns if c["stage"].kind != FunnelStage.Kind.LOST),
            "conversion": round(won * 100 / total, 1) if total else 0,
        },
        "activities": list(
            OpportunityActivity.objects.filter(opportunity__funnel=funnel)
            .select_related("stage", "actor", "opportunity__social_account")
            .order_by("-created_at")[:8]
        ),
        "tags": board_tags(funnel),
    }


def board_tags(funnel: Funnel) -> list[str]:
    tags: set[str] = set()
    for row in Opportunity.objects.filter(funnel=funnel).exclude(tags=[]).values_list("tags", flat=True)[:500]:
        tags.update(row or [])
    return sorted(tags, key=str.lower)


def candidates(workspace, funnel: Funnel, q: str = "", limit: int = 20):
    """Conversations to add to ``funnel``: those not in it yet, newest first."""
    qs = InboxMessage.objects.for_workspace(workspace.id)
    if q:
        qs = qs.filter(Q(body__icontains=q) | Q(sender_name__icontains=q) | Q(sender_handle__icontains=q))
    threads = inbox_services.latest_per_thread(qs)
    opportunities = Opportunity.objects.filter(workspace_id=OuterRef("workspace_id"), **_key_filter())
    threads = threads.annotate(opp_funnel=Subquery(opportunities.values("funnel_id")[:1]))
    if funnel.is_default:
        threads = threads.exclude(opp_funnel=funnel.id).exclude(opp_funnel__isnull=True)
    else:
        threads = threads.filter(Q(opp_funnel__isnull=True) | ~Q(opp_funnel=funnel.id))
    return list(threads.select_related("social_account").order_by("-received_at")[:limit])


# ---------------------------------------------------------------------------
# Settings: funnels and stages
# ---------------------------------------------------------------------------


def delete_stage(stage: FunnelStage) -> FunnelStage:
    """Remove ``stage``, moving its opportunities to the funnel's first other stage.

    Returns that stage. Raises ``ValueError`` when it is the funnel's only stage.
    """
    others = [s for s in stage.funnel.stages.all() if s.id != stage.id]
    if not others:
        raise ValueError(_("A funnel needs at least one stage."))
    target = others[0]
    with transaction.atomic():
        Opportunity.objects.filter(stage=stage).update(stage=target, stage_changed_at=timezone.now())
        stage.delete()
        _renumber(stage.funnel)
    return target


def reorder_stage(stage: FunnelStage, direction: int) -> None:
    stages = list(stage.funnel.stages.all())
    index = next(i for i, s in enumerate(stages) if s.id == stage.id)
    other = index + direction
    if 0 <= other < len(stages):
        stages[index], stages[other] = stages[other], stages[index]
        for position, item in enumerate(stages):
            if item.position != position:
                item.position = position
                item.save(update_fields=["position"])


def _renumber(funnel: Funnel) -> None:
    for position, item in enumerate(funnel.stages.all()):
        if item.position != position:
            item.position = position
            item.save(update_fields=["position"])
