"""Pinterest Ads screens: connect, choose (or open) the ad account, campaigns and their numbers.

Laid out like the Meta Ads ones (``apps.ads.views``), with the same rules:
reading needs ``view_analytics``; connecting and anything that changes a
campaign — which spends money — needs ``manage_social_accounts``.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, pgettext_lazy
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.accounts.impersonation import no_takeover
from apps.common.htmx import lazy_body_urls, wants_lazy_body
from apps.members.decorators import require_permission
from apps.social_accounts.views import finish_in_popup

from . import pinterest, services
from .forms import (
    PinterestAccountForm,
    PinterestAdAccountCreateForm,
    PinterestCampaignCreateForm,
    PinterestCampaignEditForm,
)
from .models import AdsConnection
from .views import _can_manage, _period, checked_callback, platform_tabs, start_sign_in

OAUTH_SESSION_KEY = "pinterest_ads_oauth"
STATE_PLATFORM = "pinterest_ads"


def _ready_connection(request, workspace_id):
    """The workspace's connection when it can be used, else where to go instead."""
    connection = pinterest.get_connection(request.workspace.id)
    if connection is None or not connection.ad_account_id:
        if connection is not None and _can_manage(request):
            return None, redirect("ads:pinterest_settings", workspace_id=workspace_id)
        return None, redirect("ads:pinterest_index", workspace_id=workspace_id)
    return connection, None


# ------------------------------------------------------------------
# Overview
# ------------------------------------------------------------------


@login_required
@require_permission("view_analytics")
@ratelimit(key="user", rate="30/m", method="GET", block=True)
@require_GET
def index(request, workspace_id):
    """Connected: the page, with a placeholder the numbers and campaigns load into
    (``overview``). Not connected: whoever can connect goes to Social Accounts."""
    connection = pinterest.get_connection(request.workspace.id)
    can_manage = _can_manage(request)
    if connection is None and can_manage:
        return redirect(f"{reverse('social_accounts:list', kwargs={'workspace_id': workspace_id})}#connect-platforms")
    if connection is not None and not connection.ad_account_id and can_manage:
        return redirect("ads:pinterest_settings", workspace_id=workspace_id)
    return render(
        request,
        "ads/pinterest/index.html",
        {
            "workspace_id": workspace_id,
            "connection": connection,
            "can_manage": can_manage,
            "period": _period(request),
            "range_choices": services.RANGE_CHOICES,
            "platform_tabs": platform_tabs(request.workspace.id, AdsConnection.Platform.PINTEREST),
        },
    )


@login_required
@require_permission("view_analytics")
@ratelimit(key="user", rate="30/m", method="GET", block=True)
@require_GET
def overview(request, workspace_id):
    """The account's numbers and campaigns, read from Pinterest; a refusal comes back
    as the page's error box, with a 200 so HTMX swaps it in."""
    connection = pinterest.get_connection(request.workspace.id)
    if connection is None or not connection.ad_account_id:
        return redirect("ads:pinterest_index", workspace_id=workspace_id)
    already_failing = connection.status == AdsConnection.Status.ERROR
    period = _period(request)
    context = {"workspace_id": workspace_id, "connection": connection, "period": period, "error": ""}
    try:
        context["overview"] = pinterest.account_overview(connection, days=period.days, end=period.end_param)
        context["campaigns"] = pinterest.list_campaigns(connection, days=period.days, end=period.end_param)
    except services.AdsError as exc:
        context["error"] = "" if already_failing else str(exc)
    return render(request, "ads/pinterest/partials/_overview.html", context)


# ------------------------------------------------------------------
# Connection
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="10/m", method="POST", block=True)
@require_POST
@finish_in_popup
def connect(request, workspace_id):
    """Send the person to Pinterest's consent screen with the ads scopes."""
    if not pinterest.is_offered():
        messages.error(request, _("This platform is not available."))
        return redirect("social_accounts:list", workspace_id=workspace_id)
    try:
        client = pinterest.client_for_org(request.org.id)
    except services.AdsNotConfiguredError as exc:
        messages.error(request, str(exc))
        return redirect("social_accounts:list", workspace_id=workspace_id)
    state = start_sign_in(request, workspace_id, session_key=OAUTH_SESSION_KEY, state_platform=STATE_PLATFORM)
    redirect_uri = request.build_absolute_uri(reverse("ads_callback:pinterest_callback"))
    return redirect(client.get_auth_url(redirect_uri, state))


@login_required
@ratelimit(key="user", rate="20/m", block=True)
@require_GET
@finish_in_popup
def callback(request):
    """Pinterest comes back here, outside any workspace URL."""
    workspace, code, elsewhere = checked_callback(
        request,
        session_key=OAUTH_SESSION_KEY,
        state_platform=STATE_PLATFORM,
        index_url_name="ads:pinterest_index",
        cancelled=_("The Pinterest sign-in was cancelled or refused."),
    )
    if elsewhere:
        return elsewhere
    try:
        pinterest.finish_sign_in(
            workspace=workspace,
            user=request.user,
            code=code,
            redirect_uri=request.build_absolute_uri(request.path),
        )
    except services.AdsError as exc:
        messages.error(request, str(exc))
        return redirect("social_accounts:list", workspace_id=workspace.id)
    messages.success(request, _("Pinterest Ads connected. Choose the ad account to use."))
    return redirect("ads:pinterest_settings", workspace_id=workspace.id)


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", block=True)
@require_http_methods(["GET", "POST"])
def connection_settings(request, workspace_id):
    """Pick the ad account, or open one. A GET shows the page at once and the
    accounts, read from Pinterest, load into it after (``partial=body``)."""
    connection = pinterest.get_connection(request.workspace.id)
    if connection is None:
        return redirect("ads:pinterest_index", workspace_id=workspace_id)
    base = {
        "workspace_id": workspace_id,
        "connection": connection,
        "create_form": PinterestAdAccountCreateForm(initial={"name": request.workspace.name}),
        **lazy_body_urls(request),
    }
    if request.method == "GET" and not wants_lazy_body(request):
        return render(request, "ads/pinterest/settings.html", base)
    body_template = (
        "ads/pinterest/partials/_settings_body.html" if request.method == "GET" else "ads/pinterest/settings.html"
    )
    try:
        accounts = pinterest.ad_accounts(connection)
    except services.AdsError as exc:
        return render(request, body_template, {**base, "form": None, "error": str(exc)})
    initial = {"ad_account_id": connection.ad_account_id or (accounts[0].id if len(accounts) == 1 else "")}
    form = PinterestAccountForm(request.POST or None, initial=initial, accounts=accounts)
    if request.method == "POST" and form.is_valid():
        try:
            pinterest.choose_account(connection, form.cleaned_data["ad_account_id"])
        except services.AdsError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Pinterest Ads settings saved."))
            return redirect("ads:pinterest_index", workspace_id=workspace_id)
    return render(request, body_template, {**base, "form": form, "accounts": accounts, "error": ""})


@login_required
@no_takeover
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="5/m", block=True)
@require_POST
def account_create(request, workspace_id):
    """Open an ad account on Pinterest for whoever signed in, and use it."""
    connection = get_object_or_404(
        AdsConnection, workspace_id=request.workspace.id, platform=AdsConnection.Platform.PINTEREST
    )
    form = PinterestAdAccountCreateForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Give the ad account a name and choose its country and currency."))
        return redirect("ads:pinterest_settings", workspace_id=workspace_id)
    try:
        pinterest.create_ad_account(connection, **form.cleaned_data)
    except services.AdsError as exc:
        messages.error(request, str(exc))
        return redirect("ads:pinterest_settings", workspace_id=workspace_id)
    messages.success(request, _("Ad account opened on Pinterest and chosen for this brand."))
    return redirect("ads:pinterest_index", workspace_id=workspace_id)


@login_required
@require_permission("manage_social_accounts")
@require_POST
def disconnect(request, workspace_id):
    """Forget the Pinterest Ads sign-in; back to Social Accounts when it came from there."""
    connection = get_object_or_404(
        AdsConnection, workspace_id=request.workspace.id, platform=AdsConnection.Platform.PINTEREST
    )
    pinterest.disconnect(connection)
    messages.success(
        request, _("Pinterest Ads disconnected. Campaigns keep running in the ad account until you stop them there.")
    )
    return redirect("social_accounts:list", workspace_id=workspace_id)


# ------------------------------------------------------------------
# Campaigns
# ------------------------------------------------------------------


@login_required
@no_takeover
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="10/m", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def campaign_new(request, workspace_id):
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    pins = pinterest.promotable_pins(request.workspace)
    choices = pinterest.targeting_choices(connection)
    form = PinterestCampaignCreateForm(
        request.POST or None, request.FILES or None, connection=connection, pins=pins, choices=choices
    )
    step = 1
    if request.method == "POST":
        if form.is_valid():
            try:
                campaign_id = pinterest.create_campaign(connection, form.to_new_campaign())
            except services.AdsError as exc:
                form.add_error(None, str(exc))
                step = len(CAMPAIGN_STEPS)
            else:
                messages.success(request, _("Campaign created, paused. Review it and activate it when you are ready."))
                return redirect("ads:pinterest_campaign_detail", workspace_id=workspace_id, campaign_id=campaign_id)
        else:
            step = form.first_step_with_errors()
    return render(
        request,
        "ads/pinterest/campaign_new.html",
        {
            "workspace_id": workspace_id,
            "connection": connection,
            "form": form,
            "pins": pins,
            "choices": choices,
            "step": step,
            "steps": CAMPAIGN_STEPS,
            "can_upload": pinterest.can_upload_images(connection),
        },
    )


# The new campaign form's steps; the form says which fields each of the first four
# holds (PinterestCampaignCreateForm.STEPS), the last is the review and the button.
CAMPAIGN_STEPS = (
    gettext_lazy("Campaign"),
    gettext_lazy("Audience"),
    gettext_lazy("Where it appears"),
    gettext_lazy("Budget and launch"),
    pgettext_lazy("wizard step", "Review"),
)


@login_required
@require_permission("view_analytics")
@ratelimit(key="user", rate="30/m", method="GET", block=True)
@require_GET
def campaign_detail(request, workspace_id, campaign_id):
    """The campaign's header (one call, inside the connected ad account) and a
    placeholder its numbers load into (``campaign_body``)."""
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    context = {
        "workspace_id": workspace_id,
        "connection": connection,
        "can_manage": _can_manage(request),
        "period": _period(request),
        "range_choices": services.RANGE_CHOICES,
        "error": "",
    }
    try:
        campaign = pinterest.get_campaign(connection, campaign_id)
    except services.AdsError as exc:
        context["error"] = str(exc)
        return render(request, "ads/pinterest/campaign_detail.html", context, status=404)
    context["campaign"] = campaign
    # A past end is left out: saving the form again must not refuse it.
    future_end = campaign.stop_time if campaign.stop_time and campaign.stop_time > timezone.now() else None
    context["edit_form"] = PinterestCampaignEditForm(
        initial={"name": campaign.name, "daily_budget": campaign.daily_budget, "end_time": future_end}
    )
    return render(request, "ads/pinterest/campaign_detail.html", context)


@login_required
@require_permission("view_analytics")
@ratelimit(key="user", rate="30/m", method="GET", block=True)
@require_GET
def campaign_body(request, workspace_id, campaign_id):
    """The campaign's numbers, ad groups and ads; checks the campaign again."""
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    period = _period(request)
    context = {"workspace_id": workspace_id, "connection": connection, "period": period, "error": ""}
    try:
        context.update(pinterest.campaign_detail(connection, campaign_id, days=period.days, end=period.end_param))
    except services.AdsError as exc:
        context["error"] = str(exc)
    return render(request, "ads/pinterest/partials/_campaign_body.html", context)


def _back_to_campaign(workspace_id, campaign_id):
    return redirect("ads:pinterest_campaign_detail", workspace_id=workspace_id, campaign_id=campaign_id)


@login_required
@no_takeover
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", block=True)
@require_POST
def campaign_status(request, workspace_id, campaign_id):
    """Activate, pause or archive. Activating is what starts spending."""
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    status = request.POST.get("status", "")
    try:
        pinterest.set_campaign_status(connection, campaign_id, status)
    except services.AdsError as exc:
        messages.error(request, str(exc))
    else:
        done = {
            "ACTIVE": _("Campaign activated. Pinterest reviews new ads before they start delivering."),
            "PAUSED": _("Campaign paused."),
            "ARCHIVED": _("Campaign archived."),
        }
        messages.success(request, done[status])
    return _back_to_campaign(workspace_id, campaign_id)


@login_required
@no_takeover
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", block=True)
@require_POST
def campaign_edit(request, workspace_id, campaign_id):
    """Rename, change the daily budget and the end."""
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    form = PinterestCampaignEditForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return _back_to_campaign(workspace_id, campaign_id)
    try:
        pinterest.update_campaign(
            connection,
            campaign_id,
            name=form.cleaned_data["name"],
            daily_budget=form.cleaned_data.get("daily_budget"),
            end_time=form.cleaned_data.get("end_time"),
        )
    except services.AdsError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Campaign updated."))
    return _back_to_campaign(workspace_id, campaign_id)
