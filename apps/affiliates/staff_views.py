"""The operator's Affiliates screen: who is an affiliate, how each performs, what is owed.

Access is ``superadmin_required`` (``apps.accounts.decorators``).
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import superadmin_required
from apps.affiliates import services
from apps.affiliates.models import Affiliate, Payout
from apps.billing.models import Coupon

logger = logging.getLogger(__name__)

RESOLVED_PAYOUTS_SHOWN = 50


@superadmin_required
@require_GET
def affiliates(request):
    rows = list(Affiliate.objects.select_related("user", "coupon"))
    figures = services.numbers_by_affiliate(rows)
    for affiliate in rows:
        affiliate.numbers = figures[affiliate.pk]
    totals = {
        key: sum(affiliate.numbers[key] for affiliate in rows)
        for key in ("clicks", "signups", "paying", "revenue_cents", "commission_cents", "available_cents", "paid_cents")
    }
    payouts = Payout.objects.select_related("affiliate", "resolved_by")
    return render(
        request,
        "affiliates/staff_affiliates.html",
        {
            "affiliates": rows,
            "totals": totals,
            "open_payouts": payouts.filter(status=Payout.Status.REQUESTED),
            "resolved_payouts": payouts.exclude(status=Payout.Status.REQUESTED)[:RESOLVED_PAYOUTS_SHOWN],
            "periods": Affiliate.Period.choices,
            "coupons": Coupon.objects.filter(is_active=True).order_by("code"),
            "admin_active": "affiliates",
        },
    )


@superadmin_required
@require_POST
def save_affiliate(request, affiliate_id=None):
    """Create an affiliate (switched off), or change one."""
    affiliate = get_object_or_404(Affiliate, pk=affiliate_id) if affiliate_id else None
    try:
        saved = services.save_affiliate(request.POST, affiliate=affiliate, created_by=request.user)
    except services.AffiliateError as exc:
        messages.error(request, str(exc))
        return redirect("affiliates_staff:affiliates")
    logger.info("Operator %s saved affiliate %s", request.user.email, saved.slug)
    if affiliate is None:
        messages.success(
            request,
            _("Affiliate %(name)s created. Switch it on to open the link and the panel.") % {"name": saved.name},
        )
    else:
        messages.success(request, _("Affiliate %(name)s saved.") % {"name": saved.name})
    return redirect("affiliates_staff:affiliates")


@superadmin_required
@require_POST
def toggle_affiliate(request, affiliate_id):
    """Switch an affiliate on or off. Off closes the link and the panel and stops new commissions."""
    affiliate = get_object_or_404(Affiliate, pk=affiliate_id)
    affiliate.is_active = not affiliate.is_active
    affiliate.save(update_fields=["is_active"])
    logger.info("Operator %s switched affiliate %s %s", request.user.email, affiliate.slug, affiliate.is_active)
    if affiliate.is_active:
        messages.success(request, _("Affiliate %(name)s is on.") % {"name": affiliate.name})
    else:
        messages.info(
            request, _("Affiliate %(name)s is off. The link and the panel are closed.") % {"name": affiliate.name}
        )
    return redirect("affiliates_staff:affiliates")


@superadmin_required
@require_POST
def resolve_payout(request, payout_id):
    """Mark a requested withdrawal paid, or reject it."""
    payout = get_object_or_404(Payout, pk=payout_id)
    paid = request.POST.get("action") == "paid"
    try:
        services.resolve_payout(payout, paid=paid, resolved_by=request.user, note=request.POST.get("note"))
    except services.AffiliateError as exc:
        messages.error(request, str(exc))
        return redirect("affiliates_staff:affiliates")
    logger.info("Operator %s resolved payout %s paid=%s", request.user.email, payout.pk, paid)
    if paid:
        messages.success(request, _("Withdrawal marked as paid."))
    else:
        messages.info(request, _("Withdrawal rejected. The amount is back in the affiliate's balance."))
    return redirect("affiliates_staff:affiliates")
