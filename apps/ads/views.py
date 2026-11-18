"""Meta Ads screens: connect, choose the ad account/Pixel/Page, campaigns and insights.

Reading (campaigns, numbers) needs ``view_analytics``. Anything that changes the
connection or a campaign — which is spending money — needs
``manage_social_accounts``, the same permission that connects accounts.
"""

from __future__ import annotations

import logging
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.common.htmx import lazy_body_urls, wants_lazy_body
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.social_accounts.views import _sign_state, _unsign_state, finish_in_popup
from apps.workspaces.models import Workspace

from . import services
from .forms import AdSetEditForm, AssetChoiceForm, CampaignCreateForm, CampaignRenameForm
from .models import AdsConnection

logger = logging.getLogger(__name__)

OAUTH_SESSION_KEY = "ads_oauth"
STATE_PLATFORM = "meta_ads"
IMAGE_PICKER_LIMIT = 48


def _days(request) -> int:
    try:
        days = int(request.GET.get("range", services.DEFAULT_RANGE))
    except (TypeError, ValueError):
        return services.DEFAULT_RANGE
    return days if days in services.RANGE_CHOICES else services.DEFAULT_RANGE


def _can_manage(request) -> bool:
    membership = request.workspace_membership
    return bool(membership and membership.effective_permissions.get("manage_social_accounts"))


def _ready_connection(request, workspace_id):
    """The workspace's connection when it can be used, else where to go instead."""
    connection = services.get_connection(request.workspace.id)
    if connection is None or not connection.ad_account_id:
        if connection is not None and _can_manage(request):
            return None, redirect("ads:settings", workspace_id=workspace_id)
        return None, redirect("ads:index", workspace_id=workspace_id)
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
    (``overview``). Not connected: whoever can connect goes to Social Accounts;
    anyone else is told it is not set up."""
    connection = services.get_connection(request.workspace.id)
    context = {
        "workspace_id": workspace_id,
        "connection": connection,
        "can_manage": _can_manage(request),
        "days": _days(request),
        "range_choices": services.RANGE_CHOICES,
    }
    if connection is None and context["can_manage"]:
        # Connecting happens on the Social Accounts page, with the other platforms.
        return redirect(f"{reverse('social_accounts:list', kwargs={'workspace_id': workspace_id})}#connect-platforms")
    if connection is not None and not connection.ad_account_id and context["can_manage"]:
        return redirect("ads:settings", workspace_id=workspace_id)
    return render(request, "ads/index.html", context)


@login_required
@require_permission("view_analytics")
@ratelimit(key="user", rate="30/m", method="GET", block=True)
@require_GET
def overview(request, workspace_id):
    """The account's numbers and campaigns, read from Meta (six calls).

    Loaded by the index page after it shows, so the person sees the page and a
    placeholder instead of a browser waiting on Meta. A Meta refusal comes back
    as the page's error box, with a 200 so HTMX swaps it in.
    """
    connection = services.get_connection(request.workspace.id)
    if connection is None or not connection.ad_account_id:
        return redirect("ads:index", workspace_id=workspace_id)
    # The index already shows the sign-in error box; a failure caused by the
    # same expired token should not repeat it.
    already_failing = connection.status == AdsConnection.Status.ERROR
    days = _days(request)
    context = {
        "workspace_id": workspace_id,
        "connection": connection,
        "days": days,
        "error": "",
    }
    try:
        context["overview"] = services.account_overview(connection, days=days)
        context["campaigns"] = services.list_campaigns(connection, days=days)
    except services.AdsError as exc:
        context["error"] = "" if already_failing else str(exc)
    return render(request, "ads/partials/_overview.html", context)


# ------------------------------------------------------------------
# Connection
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="10/m", method="POST", block=True)
@require_POST
@finish_in_popup
def connect(request, workspace_id):
    """Send the person to Meta's Login dialog with the ads permissions."""
    try:
        client = services.client_for_org(request.org.id)
    except services.AdsNotConfiguredError as exc:
        messages.error(request, str(exc))
        return redirect("ads:index", workspace_id=workspace_id)
    nonce = secrets.token_urlsafe(32)
    request.session[OAUTH_SESSION_KEY] = {"nonce": nonce, "workspace_id": str(workspace_id)}
    state = _sign_state(workspace_id, STATE_PLATFORM, request.user.id, nonce)
    redirect_uri = request.build_absolute_uri(reverse("ads_callback:meta_callback"))
    return redirect(client.get_auth_url(redirect_uri, state))


@login_required
@ratelimit(key="user", rate="20/m", block=True)
@require_GET
@finish_in_popup
def meta_callback(request):
    """Meta's Login dialog comes back here, outside any workspace URL.

    The signed state names the workspace; the session nonce, the user and the
    permission are all checked again before the token is kept.
    """
    session_data = request.session.pop(OAUTH_SESSION_KEY, {})
    fallback_workspace = session_data.get("workspace_id")
    if request.GET.get("error"):
        messages.error(request, _("The Meta sign-in was cancelled or refused."))
        if fallback_workspace:
            return redirect("ads:index", workspace_id=fallback_workspace)
        return redirect("dashboard")

    try:
        state = _unsign_state(request.GET.get("state", ""))
    except signing.BadSignature:
        messages.error(request, _("Invalid or expired OAuth state. Please try again."))
        return redirect("dashboard")
    if (
        state.get("platform") != STATE_PLATFORM
        or not session_data
        or session_data.get("nonce") != state.get("nonce")
        or session_data.get("workspace_id") != state.get("workspace_id")
    ):
        messages.error(request, _("OAuth session mismatch. Please try again."))
        return redirect("dashboard")
    if str(request.user.id) != state.get("user_id"):
        raise PermissionDenied("OAuth state does not match current user.")

    workspace_id = state["workspace_id"]
    membership = WorkspaceMembership.objects.filter(
        user=request.user, workspace_id=workspace_id, workspace__is_archived=False
    ).first()
    if membership is None or not membership.effective_permissions.get("manage_social_accounts"):
        raise PermissionDenied("You no longer have permission to manage this workspace's connections.")
    workspace = get_object_or_404(Workspace, id=workspace_id)

    code = request.GET.get("code", "")
    if not code:
        messages.error(request, _("Missing authorization code or state parameter."))
        return redirect("ads:index", workspace_id=workspace_id)
    try:
        services.finish_sign_in(
            workspace=workspace,
            user=request.user,
            code=code,
            redirect_uri=request.build_absolute_uri(request.path),
        )
    except services.AdsError as exc:
        messages.error(request, str(exc))
        return redirect("ads:index", workspace_id=workspace_id)
    messages.success(request, _("Meta Ads connected. Choose the ad account, Pixel and Page to use."))
    return redirect("ads:settings", workspace_id=workspace_id)


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", block=True)
@require_http_methods(["GET", "POST"])
def connection_settings(request, workspace_id):
    """Pick the ad account, the Pixel and the Page. ``?account=`` reloads the Pixels of another account.

    A GET shows the page at once; the lists, read from Meta (up to three calls),
    load into it after (``partial=body``). A POST that fails renders them inline.
    """
    connection = services.get_connection(request.workspace.id)
    if connection is None:
        return redirect("ads:index", workspace_id=workspace_id)
    base = {"workspace_id": workspace_id, "connection": connection, **lazy_body_urls(request)}
    if request.method == "GET" and not wants_lazy_body(request):
        return render(request, "ads/settings.html", base)
    body_template = "ads/partials/_settings_body.html" if request.method == "GET" else "ads/settings.html"
    wanted = request.POST.get("ad_account_id") or request.GET.get("account", "")
    try:
        choices = services.asset_choices(connection, wanted)
    except services.AdsError as exc:
        return render(request, body_template, {**base, "form": None, "error": str(exc)})

    same_account = bool(choices.account and choices.account.id == connection.ad_account_id)
    initial = {
        "ad_account_id": choices.account.id if choices.account else "",
        "pixel_id": connection.pixel_id if same_account else "",
        "page_id": connection.page_id if same_account else "",
    }
    if len(choices.pixels) == 1 and not initial["pixel_id"]:
        initial["pixel_id"] = choices.pixels[0].id
    if len(choices.pages) == 1 and not initial["page_id"]:
        initial["page_id"] = choices.pages[0].id
    form = AssetChoiceForm(request.POST or None, initial=initial, choices=choices)
    if request.method == "POST" and form.is_valid():
        try:
            services.choose_assets(
                connection,
                ad_account_id=form.cleaned_data["ad_account_id"],
                pixel_id=form.cleaned_data["pixel_id"],
                page_id=form.cleaned_data["page_id"],
            )
        except services.AdsError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Meta Ads settings saved."))
            return redirect("ads:index", workspace_id=workspace_id)
    return render(request, body_template, {**base, "form": form, "choices": choices, "error": ""})


@login_required
@require_permission("manage_social_accounts")
@require_POST
def disconnect(request, workspace_id):
    connection = get_object_or_404(AdsConnection, workspace_id=request.workspace.id)
    services.disconnect(connection)
    messages.success(
        request, _("Meta Ads disconnected. Campaigns keep running in the ad account until you stop them there.")
    )
    return redirect("ads:index", workspace_id=workspace_id)


# ------------------------------------------------------------------
# Campaigns
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="10/m", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def campaign_new(request, workspace_id):
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    form = CampaignCreateForm(request.POST or None, workspace=request.workspace, connection=connection)
    if request.method == "POST" and form.is_valid():
        try:
            image_bytes = services.read_image(form.cleaned_data["image"])
            campaign_id = services.create_campaign(connection, form.to_new_campaign(image_bytes))
        except services.AdsError as exc:
            form.add_error(None, str(exc))
        except OSError:
            logger.exception("Could not read media asset %s for a Meta ad", form.cleaned_data["image"].id)
            form.add_error("image", _("The image file could not be read. Choose another one."))
        else:
            messages.success(request, _("Campaign created, paused. Review it and activate it when you are ready."))
            return redirect("ads:campaign_detail", workspace_id=workspace_id, campaign_id=campaign_id)
    return render(
        request,
        "ads/campaign_new.html",
        {
            "workspace_id": workspace_id,
            "connection": connection,
            "form": form,
            # The picker shows the newest images; any image of the gallery is still accepted.
            "images": form.fields["image"].queryset.order_by("-created_at")[:IMAGE_PICKER_LIMIT],
        },
    )


@login_required
@require_permission("view_analytics")
@ratelimit(key="user", rate="30/m", method="GET", block=True)
@require_GET
def campaign_detail(request, workspace_id, campaign_id):
    """The campaign's header (one call to Meta, which also checks that the campaign
    belongs to the connected ad account) and a placeholder its numbers load into
    (``campaign_body``)."""
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    days = _days(request)
    context = {
        "workspace_id": workspace_id,
        "connection": connection,
        "can_manage": _can_manage(request),
        "days": days,
        "range_choices": services.RANGE_CHOICES,
        "error": "",
    }
    try:
        context["campaign"], _row = services.get_campaign(connection, campaign_id)
    except services.AdsError as exc:
        context["error"] = str(exc)
        return render(request, "ads/campaign_detail.html", context, status=404)
    context["rename_form"] = CampaignRenameForm(initial={"name": context["campaign"].name})
    return render(request, "ads/campaign_detail.html", context)


@login_required
@require_permission("view_analytics")
@ratelimit(key="user", rate="30/m", method="GET", block=True)
@require_GET
def campaign_body(request, workspace_id, campaign_id):
    """The campaign's numbers, where they come from, its ad sets and ads (nine calls).

    Loaded by the campaign page after it shows. Checks the campaign against the
    connected ad account again: this is a URL of its own.
    """
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    days = _days(request)
    context = {"workspace_id": workspace_id, "connection": connection, "days": days, "error": ""}
    try:
        context.update(services.campaign_detail(connection, campaign_id, days=days))
    except services.AdsError as exc:
        context["error"] = str(exc)
    return render(request, "ads/partials/_campaign_body.html", context)


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="30/m", method="GET", block=True)
@require_GET
def campaign_ad_sets(request, workspace_id, campaign_id):
    """The ad sets of the edit dialog, each with its budget and end form. Read from
    Meta only when the dialog opens; the campaign is checked again first."""
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    context = {"workspace_id": workspace_id, "connection": connection, "error": ""}
    try:
        campaign, _row = services.get_campaign(connection, campaign_id)
        ad_sets = services.campaign_ad_sets(connection, campaign)
    except services.AdsError as exc:
        context["error"] = str(exc)
    else:
        context["campaign"] = campaign
        context["ad_set_forms"] = [
            (ad_set, AdSetEditForm(initial={"daily_budget": ad_set.daily_budget}, prefix=ad_set.id))
            for ad_set in ad_sets
        ]
    return render(request, "ads/partials/_campaign_ad_sets.html", context)


def _back_to_campaign(workspace_id, campaign_id):
    return redirect("ads:campaign_detail", workspace_id=workspace_id, campaign_id=campaign_id)


@login_required
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
        services.set_campaign_status(connection, campaign_id, status)
    except services.AdsError as exc:
        messages.error(request, str(exc))
    else:
        done = {
            "ACTIVE": _("Campaign activated. Meta reviews new ads before they start delivering."),
            "PAUSED": _("Campaign paused."),
            "ARCHIVED": _("Campaign archived."),
        }
        messages.success(request, done[status])
    return _back_to_campaign(workspace_id, campaign_id)


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", block=True)
@require_POST
def campaign_rename(request, workspace_id, campaign_id):
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    form = CampaignRenameForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Give the campaign a name of up to 200 characters."))
        return _back_to_campaign(workspace_id, campaign_id)
    try:
        services.update_campaign(connection, campaign_id, name=form.cleaned_data["name"])
    except services.AdsError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Campaign renamed."))
    return _back_to_campaign(workspace_id, campaign_id)


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", block=True)
@require_POST
def ad_set_edit(request, workspace_id, campaign_id, ad_set_id):
    connection, elsewhere = _ready_connection(request, workspace_id)
    if elsewhere:
        return elsewhere
    form = AdSetEditForm(request.POST, prefix=ad_set_id)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return _back_to_campaign(workspace_id, campaign_id)
    try:
        services.update_ad_set(
            connection,
            campaign_id,
            ad_set_id,
            daily_budget=form.cleaned_data.get("daily_budget"),
            end_time=form.cleaned_data.get("end_time"),
        )
    except services.AdsError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Ad set updated."))
    return _back_to_campaign(workspace_id, campaign_id)
