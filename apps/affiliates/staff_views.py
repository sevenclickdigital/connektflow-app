"""The operator's Affiliates screen: who is an affiliate, how each performs, what is owed.

Access is ``superadmin_required`` (``apps.accounts.decorators``).
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import superadmin_required
from apps.affiliates import emails, services
from apps.affiliates.models import Affiliate, Payout
from apps.affiliates.views import receipt_response
from apps.billing.models import Coupon
from apps.media_library.validators import image_storage_filename

logger = logging.getLogger(__name__)

RESOLVED_PAYOUTS_SHOWN = 50
RECEIPT_TYPES = ("image/jpeg", "image/png", "image/webp", "application/pdf")
RECEIPT_MAX_BYTES = 5 * 1024 * 1024


@superadmin_required
@require_GET
def affiliates(request):
    rows = list(Affiliate.objects.select_related("user", "coupon"))
    figures = services.numbers_by_affiliate(rows)
    for affiliate in rows:
        affiliate.numbers = figures[affiliate.pk]
        # What the affiliate has not withdrawn: released or still inside the hold.
        affiliate.numbers["unwithdrawn_cents"] = affiliate.numbers["available_cents"] + affiliate.numbers["held_cents"]
    totals = {
        key: sum(affiliate.numbers[key] for affiliate in rows)
        for key in (
            "clicks",
            "signups",
            "paying",
            "revenue_cents",
            "commission_cents",
            "unwithdrawn_cents",
            "paid_cents",
        )
    }
    payouts = Payout.objects.select_related("affiliate", "resolved_by")
    open_payouts = list(payouts.filter(status=Payout.Status.REQUESTED))
    today = timezone.localdate()
    for payout in open_payouts:
        payout.deadline = services.payout_deadline(payout)
        payout.overdue = payout.deadline < today
    open_by_affiliate = {payout.affiliate_id: payout for payout in open_payouts}
    for affiliate in rows:
        affiliate.open_payout = open_by_affiliate.get(affiliate.pk)
    return render(
        request,
        "affiliates/staff_affiliates.html",
        {
            "affiliates": rows,
            "totals": totals,
            "open_payouts": open_payouts,
            "payout_deadline_days": services.PAYOUT_DEADLINE_DAYS,
            "resolved_payouts": payouts.exclude(status=Payout.Status.REQUESTED)[:RESOLVED_PAYOUTS_SHOWN],
            "periods": Affiliate.Period.choices,
            "accounts": services.accounts_to_choose(),
            "coupons": Coupon.objects.filter(is_active=True).order_by("code"),
            "admin_active": "affiliates",
        },
    )


@superadmin_required
@require_POST
def save_affiliate(request, affiliate_id=None):
    """Create an affiliate (switched on), or change one. The person is e-mailed their terms when they are new or changed."""
    affiliate = get_object_or_404(Affiliate, pk=affiliate_id) if affiliate_id else None
    terms_before = services.terms(affiliate) if affiliate else None
    try:
        saved = services.save_affiliate(request.POST, affiliate=affiliate, created_by=request.user)
    except services.AffiliateError as exc:
        messages.error(request, str(exc))
        return redirect("affiliates_staff:affiliates")
    logger.info("Operator %s saved affiliate %s", request.user.email, saved.slug)
    if affiliate is None:
        messages.success(
            request,
            _("Affiliate %(name)s created. The link and the panel are already open.") % {"name": saved.name},
        )
        told = emails.send_affiliate_added(saved)
    else:
        messages.success(request, _("Affiliate %(name)s saved.") % {"name": saved.name})
        told = services.terms(saved) == terms_before or emails.send_terms_changed(saved)
    if not told:
        messages.warning(
            request,
            _("The e-mail telling %(name)s about it could not be sent. Let them know yourself.") % {"name": saved.name},
        )
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
@require_GET
def payout_receipt(request, payout_id):
    """The receipt of any withdrawal."""
    return receipt_response(get_object_or_404(Payout, pk=payout_id))


@superadmin_required
@require_POST
def delete_affiliate(request, affiliate_id):
    """Delete an affiliate that has no commission and no withdrawal on record."""
    affiliate = get_object_or_404(Affiliate, pk=affiliate_id)
    try:
        services.delete_affiliate(affiliate)
    except services.AffiliateError as exc:
        messages.error(request, str(exc))
        return redirect("affiliates_staff:affiliates")
    logger.info("Operator %s deleted affiliate %s", request.user.email, affiliate.slug)
    messages.success(request, _("Affiliate %(name)s deleted.") % {"name": affiliate.name})
    return redirect("affiliates_staff:affiliates")


@superadmin_required
@require_POST
def resolve_payout(request, payout_id):
    """Mark a requested withdrawal paid with the transfer's receipt, or reject it.

    Paid, the affiliate gets an e-mail with the receipt attached.
    """
    payout = get_object_or_404(Payout, pk=payout_id)
    paid = request.POST.get("action") == "paid"
    receipt = request.FILES.get("receipt") if paid else None
    if receipt is not None:
        storage_name = image_storage_filename(receipt, RECEIPT_TYPES)
        if not storage_name or receipt.size > RECEIPT_MAX_BYTES:
            messages.error(request, _("The receipt must be a JPEG, PNG, WebP or PDF of up to 5 MB."))
            return redirect("affiliates_staff:affiliates")
        receipt.name = storage_name
    try:
        payout = services.resolve_payout(
            payout, paid=paid, resolved_by=request.user, note=request.POST.get("note"), receipt=receipt
        )
    except services.AffiliateError as exc:
        messages.error(request, str(exc))
        return redirect("affiliates_staff:affiliates")
    logger.info("Operator %s resolved payout %s paid=%s", request.user.email, payout.pk, paid)
    if paid:
        if emails.send_payout_paid(payout):
            messages.success(request, _("Withdrawal marked as paid. The affiliate got the receipt by e-mail."))
        else:
            messages.warning(
                request,
                _(
                    "Withdrawal marked as paid, but the e-mail with the receipt could not be sent. Let the affiliate know yourself."
                ),
            )
    else:
        messages.info(request, _("Withdrawal rejected. The amount is back in the affiliate's balance."))
    return redirect("affiliates_staff:affiliates")
