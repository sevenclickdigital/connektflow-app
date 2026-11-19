"""Discount coupons: their price rules, who may use them, the checkout and the staff screen."""

from __future__ import annotations

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.models import User
from apps.billing import contracting, services
from apps.billing.models import Coupon, CouponRedemption, OrganizationPlan
from apps.billing.tests.test_plan_checkout import CARD, HOLDER, StubAsaas, _prices  # noqa: F401
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

YEARLY = "yearly"


def _coupon(code="WELCOME", *, kind=Coupon.Kind.PERCENT, value=20, duration=Coupon.Duration.FIRST, **extra):
    return Coupon.objects.create(code=code, kind=kind, value=value, duration=duration, **extra)


def _org():
    org = Organization.objects.create(name="Acme")
    Workspace.objects.create(organization=org, name="Brand")
    return org


# -- price ---------------------------------------------------------------------


def test_a_coupon_on_every_charge_lowers_the_monthly_price_and_the_yearly_follows():
    quote = services.quote_plan(1, YEARLY, _coupon(duration=Coupon.Duration.FOREVER))

    assert (quote["monthly_cents"], quote["charge_cents"], quote["first_charge_cents"]) == (3200, 38400, 38400)
    assert quote["coupon_saving_cents"] == 48000 - 38400


def test_a_first_charge_coupon_lowers_only_todays_charge():
    quote = services.quote_plan(1, "monthly", _coupon(kind=Coupon.Kind.AMOUNT, value=1000))

    assert (quote["charge_cents"], quote["first_charge_cents"], quote["coupon_saving_cents"]) == (5000, 4000, 1000)


def test_no_coupon_takes_a_charge_below_what_asaas_charges():
    assert services.quote_plan(1, "monthly", _coupon(value=100))["first_charge_cents"] == 500


# -- who may use it ------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ({"is_active": False}, "does not exist"),
        ({"valid_until": datetime.date(2020, 1, 1)}, "expired"),
        ({"max_redemptions": 1, "used_by_other": True}, "run out"),
        ({"used_by_self": True}, "already used"),
    ],
)
def test_a_coupon_that_cannot_be_used_says_why(setup, message):
    org = _org()
    used_by_other, used_by_self = setup.pop("used_by_other", False), setup.pop("used_by_self", False)
    coupon = _coupon(**setup)
    if used_by_other:
        CouponRedemption.objects.create(coupon=coupon, organization=Organization.objects.create(name="Other"))
    if used_by_self:
        CouponRedemption.objects.create(coupon=coupon, organization=org)

    with translation.override("en"), pytest.raises(services.CouponError, match=message):
        services.find_coupon(" welcome ", org)


def test_a_code_typed_in_any_case_finds_the_coupon():
    coupon = _coupon()
    assert services.find_coupon("welcome", _org()) == coupon
    assert services.find_coupon("", _org()) is None


# -- checkout ------------------------------------------------------------------


def test_paying_with_a_first_charge_coupon_charges_less_today_and_records_the_use():
    org = _org()
    coupon = _coupon(kind=Coupon.Kind.AMOUNT, value=1000)
    stub = StubAsaas()

    plan = contracting.purchase_plan(
        org, brands=1, cycle="monthly", card=CARD, holder=HOLDER, remote_ip="1.1.1.1", coupon=coupon, client=stub
    )

    assert stub.subscriptions[0]["value_cents"] == 5000
    assert stub.first_values == [("pay_1", 4000)]
    assert plan.coupon == coupon
    assert CouponRedemption.objects.get(coupon=coupon, organization=org).discount_cents == 1000


def test_adding_brands_keeps_a_coupon_on_every_charge():
    org = _org()
    stub = StubAsaas()
    plan = contracting.purchase_plan(
        org,
        brands=1,
        cycle="monthly",
        card=CARD,
        holder=HOLDER,
        remote_ip="1.1.1.1",
        coupon=_coupon(duration=Coupon.Duration.FOREVER),
        client=stub,
    )

    contracting.change_plan_brands(plan, 2, client=stub)

    assert stub.values == [("sub_1", 8000)]
    assert OrganizationPlan.objects.get(pk=plan.pk).price_cents == 8000


# -- staff screen --------------------------------------------------------------


def _user(email, *, staff=False):
    return User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now(), is_staff=staff)


def test_only_staff_create_and_switch_coupons(client):
    coupon = _coupon()
    client.force_login(_user("member@example.com"))

    assert client.get(reverse("billing_staff:coupons")).status_code == 403
    assert client.post(reverse("billing_staff:create_coupon"), {"code": "FREE"}).status_code == 403
    toggle = reverse("billing_staff:toggle_coupon", kwargs={"coupon_id": coupon.id})
    assert client.post(toggle).status_code == 403
    assert Coupon.objects.get(pk=coupon.pk).is_active


def test_the_staff_screen_creates_a_coupon_and_refuses_a_bad_one(client):
    client.force_login(_user("op@example.com", staff=True))
    create = reverse("billing_staff:create_coupon")

    client.post(create, {"code": "black-friday", "kind": "amount", "value": "15,50", "duration": "forever"})
    client.post(create, {"code": "BLACK-FRIDAY", "kind": "percent", "value": "10", "duration": "first"})
    client.post(create, {"code": "X", "kind": "percent", "value": "10", "duration": "first"})
    client.post(create, {"code": "TOOMUCH", "kind": "percent", "value": "120", "duration": "first"})

    coupon = Coupon.objects.get()
    assert (coupon.code, coupon.kind, coupon.value, coupon.duration) == ("BLACK-FRIDAY", "amount", 1550, "forever")
    client.post(reverse("billing_staff:toggle_coupon", kwargs={"coupon_id": coupon.id}))
    assert not Coupon.objects.get(pk=coupon.pk).is_active
    assert "BLACK-FRIDAY" in client.get(reverse("billing_staff:coupons")).content.decode()
