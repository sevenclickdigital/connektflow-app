"""Analytics page views.

A single workspace-scoped page that picks one connected social account at a
time and renders the design's Classic layout. HTMX partials handle range
toggles, sort/filter/page changes on the All-posts table, and the
post-detail drawer fetch.
"""

from __future__ import annotations

import json
import logging
import zoneinfo

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.composer.models import PlatformPost
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.radar import reports as radar_reports
from apps.radar.models import OpportunityConnection
from apps.radar.services import describe_error
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import ProviderError

from . import history_import, services
from .metrics import PLATFORM_COLOR, PLATFORM_PRIMARY

logger = logging.getLogger(__name__)

RANGE_CHOICES = (7, 30, 90)
DEFAULT_RANGE = 30


@login_required
@require_GET
@require_permission("view_analytics")
def analytics_status(request, workspace_id, account_id):
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, pk=account_id, workspace=workspace, platform="instagram")
    return render(request, "analytics/_instagram_status.html", {"account": account, "workspace_id": workspace.id})


def _get_workspace(request, workspace_id):
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).exists():
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


def _source_accounts(workspace) -> list[OpportunityConnection]:
    """Connected opportunity sources with their own analytics (Shopee Affiliates), for the switcher."""
    return list(
        OpportunityConnection.objects.for_workspace(workspace.id)
        .filter(source=OpportunityConnection.Source.SHOPEE_AFFILIATE)
        .order_by("created_at")
    )


def _parse_range(value: str | None) -> int:
    try:
        n = int(value or DEFAULT_RANGE)
    except (TypeError, ValueError):
        return DEFAULT_RANGE
    return n if n in RANGE_CHOICES else DEFAULT_RANGE


def _parse_days_filter(value: str | None) -> int | None:
    """Parse the All-posts table's date filter: 7|30|90 or "all"."""
    if value is None or value in ("", "all"):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n in RANGE_CHOICES else None


def _parse_page(value: str | None) -> int:
    """Coerce ``?page=`` into a positive int, defaulting to 1 on bad input.

    Without this, a request like ``?page=abc`` raises ValueError and 500s
    before ``all_posts_for()`` can clamp it.
    """
    try:
        n = int(value or "1")
    except (TypeError, ValueError):
        return 1
    return max(1, n)


@login_required
@require_permission("view_analytics")
def analytics_index(request: HttpRequest, workspace_id) -> HttpResponse:
    """Landing route: redirects to the first connected account with analytics."""
    workspace = _get_workspace(request, workspace_id)
    enabled = AnalyticsPlatformConfig.enabled_platforms()
    accounts = services.analytics_accounts(workspace, enabled) if enabled else []
    if accounts:
        return redirect(
            "analytics:account", workspace_id=workspace.id, account_id=services.preferred_account(accounts).id
        )
    sources = _source_accounts(workspace)
    if sources:
        return redirect("analytics:shopee", workspace_id=workspace.id, connection_id=sources[0].id)
    if not enabled:
        raise Http404("Analytics is disabled.")
    return render(request, "analytics/no_accounts.html", {"workspace": workspace})


@login_required
@require_permission("view_analytics")
def analytics_account(request: HttpRequest, workspace_id, account_id) -> HttpResponse:
    """Main analytics page for one connected SocialAccount."""
    workspace = _get_workspace(request, workspace_id)
    enabled = AnalyticsPlatformConfig.enabled_platforms()
    if not enabled:
        raise Http404("Analytics is disabled.")

    accounts = services.analytics_accounts(workspace, enabled)
    if not accounts:
        return render(request, "analytics/no_accounts.html", {"workspace": workspace})

    account = next((a for a in accounts if str(a.id) == str(account_id)), None)
    if account is None:
        return redirect(
            "analytics:account", workspace_id=workspace.id, account_id=services.preferred_account(accounts).id
        )

    days = _parse_range(request.GET.get("range"))
    primary_color = "var(--primary)"  # locked in chat — design's locked accent

    has_any_post = PlatformPost.objects.filter(
        social_account=account,
        status=PlatformPost.Status.PUBLISHED,
    ).exists()

    # Empty / "freshly connected" state: no published posts AND no snapshot rows.
    from .models import AccountInsightsSnapshot

    has_snapshots = AccountInsightsSnapshot.objects.filter(social_account=account).exists()
    youtube_bundle = services.account_analytics_bundle(account, days) if account.platform == "youtube" else None
    if youtube_bundle is not None:
        has_snapshots = has_snapshots or bool(youtube_bundle["native_current"])
    is_fresh = not has_any_post and not has_snapshots
    # Every gate comes from the one shared predicate, which also reports which
    # of them fired — only the admin-disabled one has a fix to offer (enable,
    # then reconnect), so the template needs the cause, not just the message.
    verdict = account.analytics_verdict
    analytics_unavailable = verdict is not None

    context: dict = {
        "workspace": workspace,
        "active_account": account,
        "accounts": accounts,
        "source_accounts": _source_accounts(workspace),
        "days": days,
        "range_choices": RANGE_CHOICES,
        "primary_color": primary_color,
        "platform_color": PLATFORM_COLOR.get(account.platform, "var(--primary)"),
        # Copy for the "calculated by Connekt Flow" labels on derived metrics.
        "platform_name": services.platform_name(account),
        "content_noun": services.content_noun(account),
        "is_fresh": is_fresh,
        "reporting_period": youtube_bundle["reporting_period"] if youtube_bundle is not None else None,
        # An unavailable platform gets its own reconnect instructions inside
        # the empty state, so suppressing the scope banner here keeps one set
        # of instructions on screen instead of two competing ones.
        "analytics_needs_reconnect": account.analytics_needs_reconnect and not analytics_unavailable,
        "analytics_unavailable": analytics_unavailable,
        "analytics_unavailable_notice": verdict.message if verdict else "",
        "analytics_disabled_by_admin": bool(verdict and verdict.cause == services.CAUSE_DISABLED),
    }

    if analytics_unavailable:
        table = services.all_posts_for(
            account,
            days_filter=_parse_days_filter(request.GET.get("table_range")),
            sort_key="date",
            sort_dir=request.GET.get("dir", "desc"),
            type_filter=request.GET.get("type", "all"),
            page=_parse_page(request.GET.get("page")),
        )
        # Strip metric columns — they'd all be 0 and contradict the
        # "analytics aren't available" notice above the table.
        table["metric_labels"] = []
        context["table"] = table
        if request.headers.get("HX-Request") and request.GET.get("partial") == "table":
            return render(request, "analytics/_post_table.html", context)
        if request.headers.get("HX-Request") and request.GET.get("partial") == "page":
            return render(request, "analytics/_page.html", context)
        return render(request, "analytics/index.html", context)

    if is_fresh:
        if request.headers.get("HX-Request") and request.GET.get("partial") == "page":
            return render(request, "analytics/_page.html", context)
        return render(request, "analytics/index.html", context)

    # Compute the bundle once and thread it into every consumer — the
    # post-level fallback inside the bundle is the dominant cost, and calling
    # these helpers without ``bundle`` re-runs it 3-4 times per render.
    bundle = youtube_bundle if youtube_bundle is not None else services.account_analytics_bundle(account, days)
    follower_g = services.follower_growth(account, days, bundle=bundle)
    hero_cards = services.hero_cards(account, days, bundle=bundle)
    engagement = services.engagement_card(account, days, bundle=bundle)
    chart = services.hero_chart_data(
        account,
        days,
        metric=request.GET.get("chart_metric"),
        bundle=bundle,
    )
    table = services.all_posts_for(
        account,
        days_filter=_parse_days_filter(request.GET.get("table_range", str(days))),
        sort_key=request.GET.get("sort") or PLATFORM_PRIMARY.get(account.platform),
        sort_dir=request.GET.get("dir", "desc"),
        type_filter=request.GET.get("type", "all"),
        page=_parse_page(request.GET.get("page")),
    )

    context.update(
        {
            "follower_growth": follower_g,
            "hero_cards": hero_cards,
            "engagement": engagement,
            "calculated_note": services.calculated_metrics_note(
                account, cards=hero_cards, engagement=engagement, chart=chart, growth=follower_g
            ),
            "chart": chart,
            "reporting_period": bundle.get("reporting_period"),
            # ``null`` for days not reported yet: Chart.js draws those as a gap.
            "chart_series_json": json.dumps([None if v is None else round(v, 4) for v in chart["derived"].series]),
            "chart_labels_json": json.dumps(chart["labels"]),
            "table": table,
        }
    )

    if request.headers.get("HX-Request") and request.GET.get("partial") == "chart":
        return render(request, "analytics/_hero_chart.html", context)
    if request.headers.get("HX-Request") and request.GET.get("partial") == "table":
        return render(request, "analytics/_post_table.html", context)
    if request.headers.get("HX-Request") and request.GET.get("partial") == "page":
        # Full-page swap when account switcher is clicked via HTMX
        return render(request, "analytics/_page.html", context)

    return render(request, "analytics/index.html", context)


@login_required
@require_GET
@require_permission("view_analytics")
def shopee_report(request: HttpRequest, workspace_id, connection_id) -> HttpResponse:
    """Analytics for a Shopee Affiliates connection: the orders its links brought.

    Same page, range toggle, cards, chart and HTMX partials as a social
    account's analytics; the numbers come from ``apps.radar.reports``.
    """
    workspace = _get_workspace(request, workspace_id)
    sources = _source_accounts(workspace)
    connection = next((c for c in sources if str(c.id) == str(connection_id)), None)
    if connection is None:
        raise Http404("No such Shopee Affiliates connection.")
    enabled = AnalyticsPlatformConfig.enabled_platforms()
    days = _parse_range(request.GET.get("range"))

    context: dict = {
        "workspace": workspace,
        "accounts": services.analytics_accounts(workspace, enabled) if enabled else [],
        "source_accounts": sources,
        "active_source": connection,
        "days": days,
        "range_choices": RANGE_CHOICES,
        "primary_color": "var(--primary)",
    }
    try:
        report = radar_reports.build_report(
            connection,
            days=days,
            tz=zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC"),
            metric=request.GET.get("chart_metric"),
            status=request.GET.get("status", "all"),
            page=_parse_page(request.GET.get("page")),
        )
    except ProviderError as exc:
        logger.warning("Shopee Affiliates report failed for connection %s: %s", connection.id, exc)
        context["report_error"] = describe_error(exc)
        return render(request, "analytics/shopee.html", context)

    context.update(report)
    chart = report["chart"]
    context["chart_series_json"] = json.dumps([None if v is None else round(v, 2) for v in chart["derived"].series])
    context["chart_labels_json"] = json.dumps(chart["labels"])

    if request.headers.get("HX-Request") and request.GET.get("partial") == "chart":
        return render(request, "analytics/_hero_chart.html", context)
    if request.headers.get("HX-Request") and request.GET.get("partial") == "table":
        return render(request, "analytics/_shopee_orders.html", context)
    return render(request, "analytics/shopee.html", context)


@login_required
@require_permission("view_analytics")
def post_detail(request: HttpRequest, workspace_id, post_id) -> HttpResponse:
    """HTMX-loaded payload for the slide-over post-detail drawer."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(
        PlatformPost.objects.select_related("social_account", "post").prefetch_related(
            "post__media_attachments__media_asset"
        ),
        id=post_id,
        social_account__workspace=workspace,
    )
    context = services.post_detail(post)
    context.update({"workspace": workspace})
    return render(request, "analytics/_post_detail.html", context)


# ---------------------------------------------------------------------------
# Importing the channel's own history
# ---------------------------------------------------------------------------


@login_required
@require_GET
@require_permission("view_analytics")
def history_offer(request: HttpRequest, workspace_id, account_id) -> HttpResponse:
    """How many of this channel's own posts are not in the table yet.

    Loaded lazily by the analytics page rather than counted during its render:
    answering costs a call to the platform, and the page must not wait on it
    to show the numbers it already has. Renders nothing when there is nothing
    to offer, so the empty case adds no furniture to the page.
    """
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace)

    return render(
        request,
        "analytics/_history_offer.html",
        {
            "workspace": workspace,
            "account": account,
            "importable": history_import.importable_count(account),
            "can_import": _can_import_history(request, workspace),
        },
    )


@login_required
@require_POST
def history_import_view(request: HttpRequest, workspace_id, account_id) -> HttpResponse:
    """Import this channel's existing posts, then report what came in.

    POST because it writes. The import itself is idempotent — a double-click
    brings in nothing the second time — but the permission check is what stops
    a workspace member who cannot manage channels from spending the account's
    API quota.
    """
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace)

    if not _can_import_history(request, workspace):
        raise PermissionDenied("You cannot import history for this workspace.")

    result = history_import.import_history(account)

    # The posts and the chart are two halves of the same gap: the table was
    # empty because this product published none of these posts, and the chart
    # started at connection because nothing had measured the days before it.
    # One button closes both, because to the person looking at the page they
    # are the same complaint.
    from .tasks import backfill_account_series

    historical_rows = backfill_account_series(account)

    return render(
        request,
        "analytics/_history_offer.html",
        {
            "workspace": workspace,
            "account": account,
            # Zero now, by definition: what was importable has just been
            # imported. The panel renders its "done" state off ``result``.
            "importable": 0,
            "result": result,
            "historical_rows": historical_rows,
            "can_import": True,
        },
    )


def _can_import_history(request, workspace) -> bool:
    """Importing spends the channel's API quota, so it takes the channel permission.

    Reading analytics takes ``view_analytics``; this writes rows and makes calls
    on the account's behalf, which is the thing ``manage_social_accounts``
    governs everywhere else.
    """
    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if membership is None:
        return False
    return bool(membership.effective_permissions.get("manage_social_accounts", False))
