"""Rules of the affiliate programme: attribution, commissions and payouts.

Attribution: opening ``/af/<slug>/`` of an active affiliate leaves a signed
cookie for 30 days (the last link opened wins); the account that signs up
with it becomes that affiliate's referral. An account without a referral
that pays with an affiliate's coupon becomes one too.

Commission: each payment received from a referred organization earns the
affiliate their percentage, from the first payment until the affiliate's
period ends. Nothing is earned on a charge that was not paid, nor while the
affiliate is switched off. A refund or chargeback takes the commission back.

Payout: the affiliate asks for everything available once it reaches their
minimum; an operator pays it outside the product and marks it paid.
"""

import logging
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from dateutil.relativedelta import relativedelta
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.affiliates.models import Affiliate, AffiliateClickDay, Commission, Payout, Referral
from apps.billing import services as billing_services
from apps.billing.models import Coupon, CouponRedemption
from apps.common.validators import clean_display_name
from apps.members.models import OrgMembership

logger = logging.getLogger(__name__)

COOKIE_NAME = "cf_aff"
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
MAX_PERCENT = 100
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_MIN_LENGTH, SLUG_MAX_LENGTH = 3, 40

PAID_EVENTS = frozenset({"PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"})
#: The money went back to the payer.
TAKEN_BACK_EVENTS = frozenset(
    {"PAYMENT_REFUNDED", "PAYMENT_CHARGEBACK_REQUESTED", "PAYMENT_REVERSED", "PAYMENT_DELETED"}
)

_MIN_PAYOUT_MESSAGES = {
    "required": "Enter the minimum withdrawal.",
    "invalid": "Enter the minimum withdrawal as an amount in R$.",
}


class AffiliateError(ValueError):
    """Something the operator or the affiliate has to fix. The message is for them."""


# ---------------------------------------------------------------------------
# Affiliates
# ---------------------------------------------------------------------------


def save_affiliate(data, *, affiliate=None, created_by=None):
    """Create an affiliate, or change ``affiliate``, from the staff form's fields.

    A new affiliate is the account with the e-mail typed and starts switched
    off. Raises ``AffiliateError`` with what to fix.
    """
    slug = (data.get("slug") or "").strip().lower()
    if not (SLUG_MIN_LENGTH <= len(slug) <= SLUG_MAX_LENGTH and _SLUG.match(slug)):
        raise AffiliateError(_("The link name takes 3 to 40 lower-case letters, digits or hyphens."))
    if Affiliate.objects.filter(slug=slug).exclude(pk=affiliate.pk if affiliate else None).exists():
        raise AffiliateError(_("There is already an affiliate with this link name."))

    percent = _digits(data.get("commission_percent"))
    if not 1 <= percent <= MAX_PERCENT:
        raise AffiliateError(_("Enter the commission as a percentage from 1 to 100."))
    months = _digits(data.get("commission_months"))
    if months not in Affiliate.Period.values:
        raise AffiliateError(_("Choose for how long the affiliate earns commission."))
    try:
        min_payout_cents = billing_services._parse_currency_to_cents(data.get("min_payout") or "", _MIN_PAYOUT_MESSAGES)
    except billing_services.PriceValidationError:
        raise AffiliateError(_("Enter the minimum withdrawal as an amount in R$.")) from None

    coupon = None
    code = (data.get("coupon") or "").strip().upper()
    if code:
        coupon = Coupon.objects.filter(code=code).first()
        if coupon is None:
            raise AffiliateError(_("There is no coupon with this code. Create it on the Coupons screen first."))
        if Affiliate.objects.filter(coupon=coupon).exclude(pk=affiliate.pk if affiliate else None).exists():
            raise AffiliateError(_("This coupon already belongs to another affiliate."))

    if affiliate is None:
        email = (data.get("email") or "").strip()
        user = get_user_model().objects.filter(email__iexact=email).first() if email else None
        if user is None:
            raise AffiliateError(_("There is no account with this e-mail. The person has to sign up first."))
        if Affiliate.objects.filter(user=user).exists():
            raise AffiliateError(_("This account is already an affiliate."))
        affiliate = Affiliate(user=user, created_by=created_by)

    affiliate.name = clean_display_name(data.get("name") or "") or (affiliate.user.email if affiliate.user else slug)
    affiliate.slug = slug
    affiliate.commission_percent = percent
    affiliate.commission_months = months
    affiliate.min_payout_cents = min_payout_cents
    affiliate.coupon = coupon
    affiliate.note = (data.get("note") or "").strip()[:255]
    affiliate.save()
    return affiliate


def _digits(raw):
    text = "" if raw is None else str(raw).strip()
    return int(text) if text.isdigit() and len(text) <= 4 else 0


def active_affiliate_for(user):
    """The switched-on affiliate ``user`` is, or None."""
    if not getattr(user, "is_authenticated", False):
        return None
    return Affiliate.objects.filter(user=user, is_active=True).select_related("coupon").first()


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def remember_click(affiliate):
    """Count one opening of the affiliate's link today."""
    today = timezone.localdate()
    if not AffiliateClickDay.objects.filter(affiliate=affiliate, day=today).update(clicks=F("clicks") + 1):
        try:
            with transaction.atomic():
                AffiliateClickDay.objects.create(affiliate=affiliate, day=today, clicks=1)
        except IntegrityError:
            AffiliateClickDay.objects.filter(affiliate=affiliate, day=today).update(clicks=F("clicks") + 1)


def attribute_signup(request, user):
    """Make the organization ``user`` just signed up with a referral of the link they came by.

    Does nothing without the link's cookie, for an affiliate switched off
    since, or for the affiliate's own account.
    """
    try:
        slug = request.get_signed_cookie(COOKIE_NAME, default="", max_age=COOKIE_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    if not slug:
        return None
    affiliate = Affiliate.objects.filter(slug=slug, is_active=True).first()
    if affiliate is None or affiliate.user_id == user.pk:
        return None
    membership = (
        OrgMembership.objects.filter(user=user, org_role=OrgMembership.OrgRole.OWNER)
        .select_related("organization")
        .first()
    )
    if membership is None:
        return None
    referral, _created = Referral.objects.get_or_create(
        organization=membership.organization,
        defaults={"affiliate": affiliate, "source": Referral.Source.LINK},
    )
    return referral


def checkout_coupon_code(organization):
    """The code to fill in at ``organization``'s checkout: its affiliate's coupon, while both are on."""
    referral = (
        Referral.objects.filter(organization=organization, affiliate__is_active=True, affiliate__coupon__is_active=True)
        .select_related("affiliate__coupon")
        .first()
    )
    return referral.affiliate.coupon.code if referral else ""


def _referral_for(organization):
    """``organization``'s referral; one is made when it paid with an affiliate's coupon."""
    referral = Referral.objects.filter(organization=organization).select_related("affiliate").first()
    if referral is not None:
        return referral
    redemption = (
        CouponRedemption.objects.filter(organization=organization, coupon__affiliates__is_active=True)
        .select_related("coupon")
        .order_by("created_at")
        .first()
    )
    if redemption is None:
        return None
    affiliate = redemption.coupon.affiliates.filter(is_active=True).first()
    # An affiliate does not earn on an account of their own.
    if (
        affiliate.user_id
        and OrgMembership.objects.filter(organization=organization, user_id=affiliate.user_id).exists()
    ):
        return None
    referral, _created = Referral.objects.get_or_create(
        organization=organization,
        defaults={"affiliate": affiliate, "source": Referral.Source.COUPON},
    )
    return referral


# ---------------------------------------------------------------------------
# Commissions
# ---------------------------------------------------------------------------


def record_payment_event(organization, event_type, payload):
    """Apply one gateway payment event of ``organization`` to its affiliate's commissions.

    Called by the Asaas webhook for every payment event it matched to a plan,
    a subscription or an add-on. Safe to call again with the same event.
    """
    payment = payload.get("payment")
    if not isinstance(payment, dict) or not payment.get("id"):
        return None
    payment_id = str(payment["id"])
    if event_type in TAKEN_BACK_EVENTS:
        return _take_back(payment_id)
    if event_type not in PAID_EVENTS:
        return None

    with transaction.atomic():
        referral = _referral_for(organization)
        if referral is None or not referral.affiliate.is_active:
            return None
        existing = Commission.objects.select_for_update().filter(gateway_payment_id=payment_id).first()
        if existing is not None:
            # Paid again after a refund that had taken an unpaid commission back.
            if existing.status == Commission.Status.REVERSED:
                existing.status = Commission.Status.EARNED
                existing.save(update_fields=["status"])
            return existing

        payment_cents = _cents(payment.get("value"))
        if payment_cents <= 0:
            return None
        paid_at = _paid_at(payment)
        referral = Referral.objects.select_for_update().get(pk=referral.pk)
        affiliate = referral.affiliate
        if referral.first_paid_at is None:
            referral.first_paid_at = paid_at
            referral.commission_until = timezone.localdate(paid_at) + relativedelta(months=affiliate.commission_months)
            referral.save(update_fields=["first_paid_at", "commission_until"])
        if timezone.localdate(paid_at) > referral.commission_until:
            return None
        amount = (Decimal(payment_cents) * affiliate.commission_percent / 100).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
        return Commission.objects.create(
            affiliate=affiliate,
            referral=referral,
            gateway_payment_id=payment_id,
            paid_at=paid_at,
            payment_cents=payment_cents,
            percent=affiliate.commission_percent,
            amount_cents=int(amount),
        )


def _take_back(payment_id):
    """Undo the commission of a payment whose money went back to the payer."""
    with transaction.atomic():
        commission = (
            Commission.objects.select_for_update(of=("self",))
            .filter(gateway_payment_id=payment_id, status=Commission.Status.EARNED)
            .select_related("payout")
            .first()
        )
        if commission is None:
            return None
        payout = commission.payout
        if payout is not None and payout.status == Payout.Status.PAID:
            # Already paid out: the amount comes off what the affiliate earns next.
            reversal, _created = Commission.objects.get_or_create(
                gateway_payment_id=f"{payment_id}:reversal",
                defaults={
                    "affiliate_id": commission.affiliate_id,
                    "referral_id": commission.referral_id,
                    "paid_at": timezone.now(),
                    "payment_cents": commission.payment_cents,
                    "percent": commission.percent,
                    "amount_cents": -commission.amount_cents,
                },
            )
            return reversal
        commission.status = Commission.Status.REVERSED
        commission.payout = None
        commission.save(update_fields=["status", "payout"])
        if payout is not None:
            # Still waiting to be paid: the request shrinks by this commission.
            Payout.objects.filter(pk=payout.pk).update(
                amount_cents=max(payout.amount_cents - commission.amount_cents, 0)
            )
        return commission


def _cents(value):
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _paid_at(payment):
    """When the payer paid, from the payment's dates (plain ``YYYY-MM-DD``); now when it has none."""
    for key in ("clientPaymentDate", "paymentDate", "confirmedDate"):
        raw = payment.get(key)
        if isinstance(raw, str) and raw:
            try:
                day = datetime.strptime(raw[:10], "%Y-%m-%d")
            except ValueError:
                continue
            return timezone.make_aware(day.replace(hour=12))
    return timezone.now()


# ---------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------

_AVAILABLE = Q(payout__isnull=True) & ~Q(status=Commission.Status.REVERSED)


def request_payout(affiliate, pix_key):
    """Ask to withdraw everything available. Raises ``AffiliateError`` when it cannot be asked."""
    pix_key = " ".join((pix_key or "").split())[:140]
    if not pix_key:
        raise AffiliateError(_("Enter the Pix key to receive the payment."))
    with transaction.atomic():
        Affiliate.objects.select_for_update().get(pk=affiliate.pk)
        if Payout.objects.filter(affiliate=affiliate, status=Payout.Status.REQUESTED).exists():
            raise AffiliateError(_("You already have a withdrawal waiting to be paid."))
        commissions = Commission.objects.select_for_update().filter(_AVAILABLE, affiliate=affiliate)
        amount = sum(commission.amount_cents for commission in commissions)
        if amount < affiliate.min_payout_cents or amount <= 0:
            raise AffiliateError(_("Your balance has not reached the minimum withdrawal yet."))
        payout = Payout.objects.create(affiliate=affiliate, amount_cents=amount, pix_key=pix_key)
        Commission.objects.filter(pk__in=[commission.pk for commission in commissions]).update(payout=payout)
    logger.info("Affiliate %s asked for a payout of %s cents", affiliate.slug, amount)
    return payout


def resolve_payout(payout, *, paid, resolved_by, note=""):
    """Mark a requested payout paid, or reject it and give its commissions back to the balance."""
    with transaction.atomic():
        payout = Payout.objects.select_for_update().get(pk=payout.pk)
        if payout.status != Payout.Status.REQUESTED:
            raise AffiliateError(_("This withdrawal has already been resolved."))
        payout.status = Payout.Status.PAID if paid else Payout.Status.REJECTED
        payout.resolved_at = timezone.now()
        payout.resolved_by = resolved_by
        payout.note = (note or "").strip()[:255]
        payout.save(update_fields=["status", "resolved_at", "resolved_by", "note"])
        if not paid:
            payout.commissions.update(payout=None)
    return payout


# ---------------------------------------------------------------------------
# Numbers for the two panels
# ---------------------------------------------------------------------------


def numbers_by_affiliate(affiliates):
    """``{affiliate id: figures}`` for ``affiliates``: clicks, sign-ups, paying accounts, revenue, commission and balance.

    A fixed number of grouped queries, however many affiliates there are.
    """
    ids = [affiliate.pk for affiliate in affiliates]
    figures = {
        affiliate.pk: {
            "clicks": 0,
            "signups": 0,
            "paying": 0,
            "revenue_cents": 0,
            "commission_cents": 0,
            "available_cents": 0,
            "requested_cents": 0,
            "paid_cents": 0,
            "can_request": False,
        }
        for affiliate in affiliates
    }
    for row in AffiliateClickDay.objects.filter(affiliate__in=ids).values("affiliate").annotate(total=Sum("clicks")):
        figures[row["affiliate"]]["clicks"] = row["total"]
    for row in (
        Referral.objects.filter(affiliate__in=ids)
        .values("affiliate")
        .annotate(signups=Count("id"), paying=Count("id", filter=Q(first_paid_at__isnull=False)))
    ):
        figures[row["affiliate"]].update(signups=row["signups"], paying=row["paying"])
    for row in (
        Commission.objects.filter(affiliate__in=ids)
        .exclude(status=Commission.Status.REVERSED)
        .values("affiliate")
        .annotate(
            revenue=Sum("payment_cents", filter=Q(amount_cents__gt=0)),
            commission=Sum("amount_cents"),
            available=Sum("amount_cents", filter=Q(payout__isnull=True)),
        )
    ):
        figures[row["affiliate"]].update(
            revenue_cents=row["revenue"] or 0,
            commission_cents=row["commission"] or 0,
            available_cents=row["available"] or 0,
        )
    open_payouts = set()
    for row in (
        Payout.objects.filter(affiliate__in=ids).values("affiliate", "status").annotate(total=Sum("amount_cents"))
    ):
        if row["status"] == Payout.Status.REQUESTED:
            figures[row["affiliate"]]["requested_cents"] = row["total"]
            open_payouts.add(row["affiliate"])
        elif row["status"] == Payout.Status.PAID:
            figures[row["affiliate"]]["paid_cents"] = row["total"]
    for affiliate in affiliates:
        own = figures[affiliate.pk]
        own["can_request"] = own["available_cents"] >= affiliate.min_payout_cents and affiliate.pk not in open_payouts
    return figures


def numbers(affiliate):
    """The figures of one affiliate, for their panel."""
    return numbers_by_affiliate([affiliate])[affiliate.pk]
