"""The affiliate's link and their own panel.

The link is public. The panel is for an account an operator made an
affiliate and switched on; anyone else gets a 404, so the programme is not
advertised to accounts that are not part of it.
"""

import functools

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.affiliates import services
from apps.affiliates.models import Affiliate, Commission

REFERRALS_SHOWN = 200
COMMISSIONS_SHOWN = 100


@require_GET
def link(request, slug):
    """Open an affiliate's link: count the visit, remember the affiliate, go to sign-up."""
    response = redirect("dashboard" if request.user.is_authenticated else "account_signup")
    affiliate = Affiliate.objects.filter(slug=slug.lower(), is_active=True).first()
    if affiliate is None or request.user.is_authenticated:
        return response
    services.remember_click(affiliate)
    response.set_signed_cookie(
        services.COOKIE_NAME,
        affiliate.slug,
        max_age=services.COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure(),
    )
    return response


def affiliate_required(view_func):
    """Log in, and be a switched-on affiliate; ``request.affiliate`` is set."""

    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        affiliate = services.active_affiliate_for(request.user)
        if affiliate is None:
            raise Http404
        request.affiliate = affiliate
        return view_func(request, *args, **kwargs)

    return login_required(_wrapped)


@affiliate_required
@require_GET
def panel(request):
    """The affiliate's numbers, the accounts they brought, their commissions and withdrawals."""
    affiliate = request.affiliate
    earned = ~Q(commissions__status=Commission.Status.REVERSED)
    referrals = (
        affiliate.referrals.select_related("organization__billing_plan")
        .annotate(earned_cents=Sum("commissions__amount_cents", filter=earned))
        .order_by("-created_at")[:REFERRALS_SHOWN]
    )
    return render(
        request,
        "affiliates/panel.html",
        {
            "affiliate": affiliate,
            "numbers": services.numbers(affiliate),
            "link": request.build_absolute_uri(reverse("affiliate_link", kwargs={"slug": affiliate.slug})),
            "referrals": referrals,
            "commissions": affiliate.commissions.select_related("referral__organization")[:COMMISSIONS_SHOWN],
            "payouts": affiliate.payouts.all(),
        },
    )


@affiliate_required
@require_POST
def request_payout(request):
    try:
        services.request_payout(request.affiliate, request.POST.get("pix_key"))
    except services.AffiliateError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Withdrawal requested. We will pay it to the Pix key you entered."))
    return redirect("affiliates:panel")
