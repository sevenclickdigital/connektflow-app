"""The operator's screen for market hashtags: the hashtags of each segment
and the Instagram account they are read through.

Access is ``staff_required`` (``apps.accounts.decorators``).
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import staff_required
from apps.calendar.models import DateSegment
from apps.social_accounts.models import SocialAccount

from . import instagram, market_hashtags
from .models import MarketHashtag, MarketHashtagCollector
from .tasks import collect_market_hashtags


def _back():
    return redirect("radar_staff:market_hashtags")


def _operator_accounts(user):
    """Instagram accounts the operator may read through: connected with
    Facebook Login (the only kind Meta serves hashtag search to) in a brand
    the operator belongs to. Never a customer's account."""
    return (
        SocialAccount.objects.filter(platform="instagram", workspace__memberships__user=user)
        .exclude(connection_status=SocialAccount.ConnectionStatus.DISCONNECTED)
        .select_related("workspace")
        .distinct()
        .order_by("account_name")
    )


@staff_required
@require_GET
def hashtags(request):
    rows = list(MarketHashtag.objects.select_related("segment").order_by("source", "-posts", "tag"))
    by_segment = {}
    for row in rows:
        by_segment.setdefault(row.segment_id, []).append(row)
    shelves = [{"segment": None, "hashtags": by_segment.get(None, [])}]
    shelves += [{"segment": s, "hashtags": by_segment.get(s.id, [])} for s in DateSegment.objects.order_by("name")]
    return render(
        request,
        "radar/staff_market_hashtags.html",
        {
            "admin_active": "market_hashtags",
            "shelves": shelves,
            "status": market_hashtags.collection_status(),
            "accounts": _operator_accounts(request.user),
            "seed_budget": market_hashtags.SEED_BUDGET,
        },
    )


@staff_required
@require_POST
def hashtag_add(request):
    segment = None
    if request.POST.get("segment"):
        segment = get_object_or_404(DateSegment, id=request.POST["segment"])
    try:
        market_hashtags.add_hashtag(segment, request.POST.get("tag", ""))
    except instagram.InvalidInputError as exc:
        messages.error(request, str(exc))
    return _back()


@staff_required
@require_POST
def hashtag_delete(request, hashtag_id):
    get_object_or_404(MarketHashtag, id=hashtag_id).delete()
    return _back()


@staff_required
@require_POST
def collector_account(request):
    """Choose the account the hashtags are read through, or none."""
    collector = MarketHashtagCollector.load()
    chosen = request.POST.get("account", "")
    collector.account = _operator_accounts(request.user).filter(id=chosen).first() if chosen else None
    collector.last_error = ""
    collector.save(update_fields=["account", "last_error"])
    return _back()


@staff_required
@require_POST
def collect_now(request):
    """Queue a reading now, beside the weekly one."""
    if MarketHashtagCollector.load().account_id is None:
        messages.error(request, _("Choose an Instagram account first."))
        return _back()
    collect_market_hashtags(verbose_name="collect_market_hashtags_now")
    messages.success(request, _("Reading queued. The result appears here in a few minutes."))
    return _back()
