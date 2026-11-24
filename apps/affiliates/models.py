"""Partners who bring customers in and earn a share of what those customers pay.

- ``Affiliate``: a partner, created by an operator, who can switch it off.
  Their link is ``/af/<slug>/``; an optional ``billing.Coupon`` is filled in at
  the checkout of the accounts they bring.
- ``AffiliateClickDay``: how many times the link was opened, per day.
- ``Referral``: an organization an affiliate brought, by the link (a cookie
  read at sign-up) or by paying with the affiliate's coupon. One per
  organization, for good.
- ``Commission``: the affiliate's share of one payment received from a
  referred organization (every payment of a period, or only the first one,
  by ``Affiliate.commission_mode``), keyed by the gateway's payment id. Written by the
  Asaas webhook (``services.record_payment_event``).
- ``Payout``: a withdrawal the affiliate asked for, holding the commissions
  it pays.

Money is integer cents, as in ``apps.billing``.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class Affiliate(models.Model):
    #: For how many months after a referred account's first payment its
    #: payments earn commission.
    class Period(models.IntegerChoices):
        QUARTER = 3, _("3 months")
        SEMESTER = 6, _("6 months")
        YEAR = 12, _("12 months")
        TWO_YEARS = 24, _("24 months")

    #: What a referred account earns the affiliate.
    class Mode(models.TextChoices):
        # A share of every payment, during ``commission_months``.
        RECURRING = "recurring", _("Monthly, on every payment")
        # A share of the account's first payment and nothing after it.
        ONCE = "once", _("Once per account")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # NULL once the person's account is deleted: what they earned and were
    # paid stays on record under ``name``.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="affiliate",
    )
    name = models.CharField(max_length=100)
    # The last part of the link, lower case.
    slug = models.CharField(max_length=40, unique=True)
    is_active = models.BooleanField(default=False)
    # db_default keeps inserts from the release still running during a deploy working.
    commission_mode = models.CharField(
        max_length=10, choices=Mode.choices, default=Mode.RECURRING, db_default=Mode.RECURRING
    )
    commission_percent = models.PositiveSmallIntegerField(default=20)
    commission_months = models.PositiveSmallIntegerField(choices=Period.choices, default=Period.YEAR)
    # The least the affiliate can ask to withdraw.
    min_payout_cents = models.PositiveIntegerField(default=5000)
    coupon = models.ForeignKey(
        "billing.Coupon",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="affiliates",
    )
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_affiliates",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.slug


class AffiliateClickDay(models.Model):
    affiliate = models.ForeignKey(Affiliate, on_delete=models.CASCADE, related_name="click_days")
    day = models.DateField()
    clicks = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["affiliate", "day"], name="affiliates_click_once_per_day"),
        ]


class Referral(models.Model):
    class Source(models.TextChoices):
        LINK = "link", _("Link")
        COUPON = "coupon", _("Coupon")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    affiliate = models.ForeignKey(Affiliate, on_delete=models.CASCADE, related_name="referrals")
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="referral",
    )
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.LINK)
    created_at = models.DateTimeField(auto_now_add=True)
    # ``first_paid_at`` is set by the first payment received. For a monthly
    # affiliate, payments up to ``commission_until`` (inclusive) earn
    # commission; it stays empty under an affiliate paid once per account.
    first_paid_at = models.DateTimeField(null=True, blank=True)
    commission_until = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Payout(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "requested", _("Requested")
        PAID = "paid", _("Paid")
        REJECTED = "rejected", _("Rejected")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    affiliate = models.ForeignKey(Affiliate, on_delete=models.CASCADE, related_name="payouts")
    amount_cents = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REQUESTED)
    # Where the affiliate asked to be paid.
    pix_key = models.CharField(max_length=140)
    note = models.CharField(max_length=255, blank=True, default="")
    # The proof of the Pix transfer (an image or a PDF), sent to the affiliate
    # when the payout is marked paid. Private: served only by the views that
    # check who asks (``affiliate_payouts/`` is not in PUBLIC_MEDIA_PREFIXES).
    receipt = models.FileField(upload_to="affiliate_payouts/%Y/%m/", blank=True, default="", db_default="")
    requested_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_affiliate_payouts",
    )

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["affiliate"],
                condition=Q(status="requested"),
                name="affiliates_one_open_payout",
            ),
        ]


class Commission(models.Model):
    class Status(models.TextChoices):
        EARNED = "earned", _("Earned")
        # The payment was refunded or charged back before the commission was paid out.
        REVERSED = "reversed", _("Reversed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    affiliate = models.ForeignKey(Affiliate, on_delete=models.CASCADE, related_name="commissions")
    # NULL once the referred organization is deleted; the commission stays.
    referral = models.ForeignKey(
        Referral,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="commissions",
    )
    # The gateway's payment id; a payment refunded after its commission was
    # paid out gets a second, negative row keyed ``<payment id>:reversal``.
    gateway_payment_id = models.CharField(max_length=80, unique=True)
    paid_at = models.DateTimeField()
    payment_cents = models.PositiveIntegerField()
    percent = models.PositiveSmallIntegerField()
    # Negative on a reversal row.
    amount_cents = models.IntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.EARNED)
    payout = models.ForeignKey(
        Payout,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="commissions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_at"]
