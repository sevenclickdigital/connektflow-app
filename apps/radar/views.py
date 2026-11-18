"""Radar: opportunity sources (Shopee Affiliates first) and their offers."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.billing.storage_usage import WorkspaceStorageExceededError
from apps.common.htmx import toast_response
from apps.media_library.quotas import StorageQuotaExceededError
from apps.members.decorators import require_permission
from providers.exceptions import OAuthError, ProviderError

from . import instagram as ig_research
from . import services
from .forms import OfferFilterForm, ShopeeConnectForm
from .models import OpportunityConnection, WatchedProfile
from .sources import source_cards

logger = logging.getLogger(__name__)

# Generous ceiling on a caption typed in the share dialog; each platform's own
# limit is enforced by the composer when the post is scheduled.
MAX_CAPTION_LENGTH = 10_000


@login_required
@require_permission("create_posts")
@require_GET
def index(request, workspace_id):
    return render(
        request,
        "radar/index.html",
        {
            "workspace_id": workspace_id,
            "cards": source_cards(workspace_id),
            "has_instagram": bool(ig_research.research_accounts(workspace_id)),
        },
    )


@login_required
@require_permission("create_posts")
@ratelimit(key="user", rate="30/m", method="GET", block=True)
@require_GET
def instagram(request, workspace_id):
    """Watched profiles and hashtag search, read through one of the workspace's Instagram accounts.

    ``tab`` picks the panel, ``account`` the account to read through,
    ``u`` the watched profile to open and ``tag``/``order`` the hashtag search.
    HTMX requests get only the results block.
    """
    accounts = ig_research.research_accounts(request.workspace.id)
    account = next((a for a in accounts if str(a.id) == request.GET.get("account")), accounts[0] if accounts else None)
    tab = "hashtags" if request.GET.get("tab") == "hashtags" else "profiles"
    context = {
        "workspace_id": workspace_id,
        "accounts": accounts,
        "account": account,
        "tab": tab,
        "watched": WatchedProfile.objects.for_workspace(request.workspace.id),
        "error": "",
    }
    if account and tab == "profiles" and request.GET.get("u"):
        context["handle"] = request.GET["u"].strip().lstrip("@")
        try:
            context["report"] = ig_research.profile_report(account, request.GET["u"])
        except ig_research.InvalidInputError as exc:
            context["error"] = str(exc)
        except ProviderError as exc:
            logger.warning("Business Discovery refused for account %s: %s", account.id, exc)
            context["error"] = ig_research.describe_error(exc, kind="profile")
    elif account and tab == "hashtags" and request.GET.get("tag"):
        context["hashtag"] = request.GET["tag"].strip().lstrip("#")
        context["order"] = "recent" if request.GET.get("order") == "recent" else "top"
        try:
            context["posts"] = ig_research.hashtag_posts(account, request.GET["tag"], order=context["order"])
        except ig_research.InvalidInputError as exc:
            context["error"] = str(exc)
        except ProviderError as exc:
            logger.warning("Hashtag search refused for account %s: %s", account.id, exc)
            context["error"] = ig_research.describe_error(exc, kind="hashtag")
    if request.htmx:
        return render(request, "radar/partials/_instagram_results.html", context)
    return render(request, "radar/instagram.html", context)


@login_required
@require_permission("create_posts")
@require_POST
def watch_profile(request, workspace_id):
    """Add a handle to the workspace's watched profiles and open it."""
    try:
        profile = ig_research.watch_profile(request.workspace, request.POST.get("username", ""), request.user)
    except ig_research.InvalidInputError as exc:
        messages.error(request, str(exc))
        return redirect("radar:instagram", workspace_id=workspace_id)
    query = {"u": profile.username}
    if request.POST.get("account"):
        query["account"] = request.POST["account"]
    return redirect(f"{reverse('radar:instagram', kwargs={'workspace_id': workspace_id})}?{urlencode(query)}")


@login_required
@require_permission("create_posts")
@require_POST
def unwatch_profile(request, workspace_id, profile_id):
    profile = get_object_or_404(WatchedProfile, id=profile_id, workspace_id=request.workspace.id)
    profile.delete()
    messages.success(request, _("@%(username)s is no longer watched.") % {"username": profile.username})
    return redirect("radar:instagram", workspace_id=workspace_id)


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="10/m", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def connect_shopee(request, workspace_id):
    """Link Shopee Affiliates with the App ID and Secret from the affiliate Open API panel.

    HTMX requests come from the source card's modal: they get the bare form back
    (with its errors) and, on success, an ``HX-Redirect`` to the offers screen.
    """
    form = ShopeeConnectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.connect_shopee(
                workspace=request.workspace,
                user=request.user,
                app_id=form.cleaned_data["app_id"],
                secret=form.cleaned_data["secret"],
            )
        except OAuthError:
            form.add_error(None, _("Shopee refused this App ID and Secret. Copy them again from the affiliate panel."))
        except ProviderError as exc:
            logger.warning("Shopee Affiliate connection check failed: %s", exc)
            form.add_error(None, services.describe_error(exc))
        else:
            messages.success(request, _("Shopee Affiliates connected."))
            if request.htmx:
                return HttpResponse(
                    headers={"HX-Redirect": reverse("radar:shopee", kwargs={"workspace_id": workspace_id})}
                )
            return redirect("radar:shopee", workspace_id=workspace_id)
    context = {"workspace_id": workspace_id, "form": form, "in_modal": bool(request.htmx)}
    if request.htmx:
        return render(request, "radar/partials/_shopee_connect_form.html", context)
    return render(request, "radar/shopee_connect.html", context)


@login_required
@require_permission("manage_social_accounts")
@require_POST
def disconnect(request, workspace_id, connection_id):
    connection = get_object_or_404(OpportunityConnection, id=connection_id, workspace_id=request.workspace.id)
    label = connection.get_source_display()
    connection.delete()
    messages.success(request, _("%(source)s disconnected.") % {"source": label})
    if request.POST.get("return_to") == "connect":
        return redirect("social_accounts:connect", workspace_id=workspace_id)
    if request.POST.get("return_to") == "radar":
        return redirect("radar:index", workspace_id=workspace_id)
    return redirect("social_accounts:list", workspace_id=workspace_id)


@login_required
@require_permission("create_posts")
@require_GET
def shopee(request, workspace_id):
    """The Shopee offers screen. HTMX requests get only the results block."""
    connection = _shopee_connection(request)
    if connection is None:
        messages.info(request, _("Connect Shopee Affiliates to see offers."))
        return redirect("radar:index", workspace_id=workspace_id)

    form = OfferFilterForm(request.GET)
    filters = form.to_filters()
    results = None
    error = ""
    try:
        results = services.list_offers(connection, filters)
    except ProviderError as exc:
        logger.warning("Shopee Affiliate offers failed for workspace %s: %s", workspace_id, exc)
        error = services.describe_error(exc)

    query = {k: v for k, v in request.GET.items() if k != "pages" and v}
    context = {
        "workspace_id": workspace_id,
        "connection": connection,
        "filters": filters,
        "active_sort": services.effective_sort(filters),
        "results": results,
        "error": error,
        "tab": "offers",
        "favorite_ids": services.favorite_ids(request.workspace.id, connection.source),
        "video_platforms": services.VIDEO_PLATFORMS,
        "message_templates": services.MESSAGE_TEMPLATES,
        "share_data": services.share_payload(results.offers) if results else {},
        "next_page_query": urlencode({**query, "pages": filters.pages + 1}),
        "categories": services.SHOPEE_CATEGORIES,
        "sort_choices": services.SORT_CHOICES,
        "quick_filters": services.QUICK_FILTERS,
        "advanced_count": sum(
            1
            for k in ("min_price", "max_price", "min_discount", "min_rating", "min_sales", "min_commission")
            if request.GET.get(k)
        ),
    }
    if request.htmx:
        return render(request, "radar/partials/_offers.html", context)
    return render(request, "radar/shopee.html", context)


@login_required
@require_permission("create_posts")
@require_GET
def shopee_favorites(request, workspace_id):
    """Starred offers, from their snapshots: they stay here after Shopee stops listing them."""
    connection = _shopee_connection(request)
    if connection is None:
        messages.info(request, _("Connect Shopee Affiliates to see offers."))
        return redirect("radar:index", workspace_id=workspace_id)
    favorites = services.favorite_offers(request.workspace.id, connection.source)
    offers = [offer for offer, _saved in favorites]
    return render(
        request,
        "radar/shopee.html",
        {
            "workspace_id": workspace_id,
            "connection": connection,
            "tab": "favorites",
            "favorites": favorites,
            "favorite_ids": {offer.item_id for offer in offers},
            "video_platforms": services.VIDEO_PLATFORMS,
            "message_templates": services.MESSAGE_TEMPLATES,
            "share_data": services.share_payload(offers),
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def offer_favorite(request, workspace_id):
    """Star or unstar an offer; answers with the star button in its new state."""
    connection = _shopee_connection(request)
    item_id = _item_id(request)
    if connection is None or item_id is None:
        return HttpResponse(status=400)
    try:
        saved = services.toggle_favorite(connection, item_id, request.user)
    except services.OfferUnavailableError:
        return _offer_gone()
    return render(
        request,
        "radar/partials/_favorite_button.html",
        {"workspace_id": workspace_id, "item_id": item_id, "is_favorite": saved},
    )


@login_required
@require_permission("create_posts")
@ratelimit(key="user", rate="30/m", method="POST", block=True)
@require_POST
def offer_task(request, workspace_id):
    """ "Create task" from the share dialog: the board's new task window, filled from the offer.

    Opened with ``prefill=1`` (and the dialog's text as ``caption``) it answers the window
    (tasks.views.render_task_window), which posts back here; submitted, it creates the task
    with the product image and a draft post attached, like the board's window would.
    """
    from apps.tasks import services as task_services
    from apps.tasks.views import parse_task_post, render_task_window

    connection = _shopee_connection(request)
    item_id = _item_id(request)
    if connection is None or item_id is None:
        return HttpResponse(status=400)
    try:
        offer = services.find_offer(connection, item_id)
    except services.OfferUnavailableError:
        return _offer_gone()

    if request.POST.get("prefill"):
        prefill = task_services.material_prefill(
            request.workspace,
            title=_("Post offer: %(name)s") % {"name": offer.name},
            briefing=services.offer_brief(offer),
            post_caption=_caption(request, offer),
        )
        return render_task_window(
            request,
            prefill,
            action_url=reverse("radar:offer_task", kwargs={"workspace_id": workspace_id}),
            hidden={"item_id": item_id},
            source_note=_("From the Shopee offer “%(name)s”") % {"name": offer.name},
        )

    fields, art, error = parse_task_post(request)
    if error:
        return toast_response(tone="error", title=error)
    caption = fields["post_caption"][:MAX_CAPTION_LENGTH] or services.share_texts(offer)["direct"]
    try:
        services.create_offer_task(connection, offer, caption, request.user, art=art, **fields)
    except _IMAGE_ERRORS as exc:
        title_msg, body = _image_error(exc, workspace_id)
        return toast_response(tone="error", title=title_msg, body=body)
    return toast_response(
        tone="success",
        title=_("Task created with the product image and a draft post."),
        events={"tasksChanged": True, "taskCreated": True},
    )


@login_required
@require_permission("create_posts")
@ratelimit(key="user", rate="20/m", method="POST", block=True)
@require_POST
def offer_post(request, workspace_id):
    """Create a draft post with the offer's text and image and open it in the composer to schedule."""
    connection = _shopee_connection(request)
    item_id = _item_id(request)
    if connection is None or item_id is None:
        return HttpResponse(status=400)
    try:
        offer = services.find_offer(connection, item_id)
    except services.OfferUnavailableError:
        return _offer_gone()
    try:
        post = services.create_offer_post(connection, offer, _caption(request, offer), request.user)
    except _IMAGE_ERRORS as exc:
        title_msg, body = _image_error(exc, workspace_id)
        return toast_response(tone="error", title=title_msg, body=body)
    return HttpResponse(
        headers={
            "HX-Redirect": reverse("composer:compose_edit", kwargs={"workspace_id": workspace_id, "post_id": post.id})
        }
    )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _shopee_connection(request) -> OpportunityConnection | None:
    return (
        OpportunityConnection.objects.for_workspace(request.workspace.id)
        .filter(source=OpportunityConnection.Source.SHOPEE_AFFILIATE)
        .first()
    )


def _item_id(request) -> int | None:
    try:
        value = int(request.POST.get("item_id", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _caption(request, offer) -> str:
    """The text edited in the share dialog, or the default template when it came empty."""
    return (request.POST.get("caption") or "").strip()[:MAX_CAPTION_LENGTH] or services.share_texts(offer)["direct"]


def _offer_gone():
    return toast_response(
        tone="error",
        title=_("This offer is no longer loaded"),
        body=_("Reload the offers and try again."),
    )


# Failures copying the product image into the media library.
_IMAGE_ERRORS = (ProviderError, StorageQuotaExceededError, WorkspaceStorageExceededError, ValueError, ValidationError)


def _image_error(exc: Exception, workspace_id) -> tuple[str, str]:
    """(title, body) telling the user why the product image could not be copied."""
    if isinstance(exc, (StorageQuotaExceededError, WorkspaceStorageExceededError)):
        return _("Media storage is full"), _("Free up space in the media library to copy the product image.")
    logger.warning("Shopee offer image import failed for workspace %s: %s", workspace_id, exc)
    if isinstance(exc, ProviderError):
        return _("Could not copy the product image"), services.describe_error(exc)
    return _("Could not copy the product image"), _("Shopee sent an image the media library does not accept.")
