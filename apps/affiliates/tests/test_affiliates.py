"""The affiliate programme: who counts as whose referral, what a payment earns, what can be withdrawn."""

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.affiliates import services
from apps.affiliates.models import Affiliate, AffiliateClickDay, Commission, Payout, Referral
from apps.billing.models import Coupon, CouponRedemption, OrganizationPlan
from apps.members.models import OrgMembership
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db

TOKEN = "fake-webhook-token"


@pytest.fixture
def partner():
    return User.objects.create_user(email="ana@parceira.example", password="x", tos_accepted_at=timezone.now())


@pytest.fixture
def affiliate(partner):
    return Affiliate.objects.create(
        user=partner, name="Ana", slug="ana", is_active=True, commission_percent=20, commission_months=3
    )


@pytest.fixture
def customer():
    """An organization with a plan on gateway subscription ``sub_1``."""
    organization = Organization.objects.create(name="Padaria Sol")
    OrganizationPlan.objects.create(organization=organization, gateway_subscription_id="sub_1")
    return organization


def _event(event, payment_id="pay_1", value=100.0, paid_on="2026-11-10"):
    return {
        "id": f"evt_{event}_{payment_id}",
        "event": event,
        "payment": {"id": payment_id, "subscription": "sub_1", "value": value, "paymentDate": paid_on},
    }


def _pay(customer, **kwargs):
    return services.record_payment_event(customer, "PAYMENT_RECEIVED", _event("PAYMENT_RECEIVED", **kwargs))


class TestTheLink:
    def test_an_active_affiliate_s_link_is_counted_and_remembered(self, client, affiliate):
        response = client.get(reverse("affiliate_link", kwargs={"slug": "ana"}))

        assert response.headers["Location"] == reverse("account_signup")
        assert services.COOKIE_NAME in response.cookies
        assert AffiliateClickDay.objects.get(affiliate=affiliate).clicks == 1

    def test_a_link_that_is_off_remembers_nobody(self, client, affiliate):
        Affiliate.objects.update(is_active=False)

        response = client.get(reverse("affiliate_link", kwargs={"slug": "ana"}))

        assert response.status_code == 302
        assert services.COOKIE_NAME not in response.cookies
        assert not AffiliateClickDay.objects.exists()

    def test_the_account_that_signs_up_after_the_link_is_the_affiliate_s(self, client, affiliate):
        client.get(reverse("affiliate_link", kwargs={"slug": "ana"}))
        request = RequestFactory().get("/")
        request.COOKIES[services.COOKIE_NAME] = client.cookies[services.COOKIE_NAME].value
        owner = User.objects.create_user(email="dona@padaria.example", password="x")
        organization = Organization.objects.create(name="Padaria Sol")
        OrgMembership.objects.create(user=owner, organization=organization, org_role=OrgMembership.OrgRole.OWNER)

        services.attribute_signup(request, owner)

        referral = Referral.objects.get()
        assert (referral.affiliate, referral.organization, referral.source) == (affiliate, organization, "link")


class TestCommissions:
    def test_a_payment_received_earns_the_percentage_once(self, affiliate, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)

        _pay(customer)
        services.record_payment_event(customer, "PAYMENT_CONFIRMED", _event("PAYMENT_CONFIRMED"))

        commission = Commission.objects.get()
        assert (commission.payment_cents, commission.percent, commission.amount_cents) == (10000, 20, 2000)
        assert str(Referral.objects.get().commission_until) == "2027-02-10"

    def test_nothing_is_earned_without_a_referral_or_for_an_affiliate_that_is_off(self, affiliate, customer):
        _pay(customer)
        Referral.objects.create(affiliate=affiliate, organization=customer)
        Affiliate.objects.update(is_active=False)
        _pay(customer, payment_id="pay_2")

        assert not Commission.objects.exists()

    def test_an_unpaid_charge_earns_nothing(self, affiliate, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)

        services.record_payment_event(customer, "PAYMENT_OVERDUE", _event("PAYMENT_OVERDUE"))

        assert not Commission.objects.exists()

    def test_payments_after_the_period_earn_nothing(self, affiliate, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)

        _pay(customer)
        _pay(customer, payment_id="pay_2", paid_on="2027-02-10")
        _pay(customer, payment_id="pay_3", paid_on="2027-02-11")

        assert set(Commission.objects.values_list("gateway_payment_id", flat=True)) == {"pay_1", "pay_2"}

    def test_paying_with_the_affiliate_s_coupon_makes_the_account_theirs(self, affiliate, customer):
        coupon = Coupon.objects.create(code="ANA10", value=10)
        Affiliate.objects.update(coupon=coupon)
        CouponRedemption.objects.create(coupon=coupon, organization=customer)

        _pay(customer)

        assert Referral.objects.get().source == "coupon"
        assert Commission.objects.get().amount_cents == 2000

    def test_the_affiliate_s_own_account_does_not_earn_by_coupon(self, affiliate, partner, customer):
        coupon = Coupon.objects.create(code="ANA10", value=10)
        Affiliate.objects.update(coupon=coupon)
        CouponRedemption.objects.create(coupon=coupon, organization=customer)
        OrgMembership.objects.create(user=partner, organization=customer, org_role=OrgMembership.OrgRole.OWNER)

        _pay(customer)

        assert not Referral.objects.exists()

    def test_a_refund_takes_an_unpaid_commission_back(self, affiliate, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer)

        services.record_payment_event(customer, "PAYMENT_REFUNDED", _event("PAYMENT_REFUNDED"))

        assert Commission.objects.get().status == "reversed"
        assert services.numbers(affiliate)["available_cents"] == 0

    def test_a_refund_after_the_payout_comes_off_the_next_balance(self, affiliate, customer, partner):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=300.0)
        payout = services.request_payout(affiliate, "ana@pix.example")
        services.resolve_payout(payout, paid=True, resolved_by=partner)

        services.record_payment_event(customer, "PAYMENT_REFUNDED", _event("PAYMENT_REFUNDED"))
        services.record_payment_event(customer, "PAYMENT_REFUNDED", _event("PAYMENT_REFUNDED"))

        assert services.numbers(affiliate)["available_cents"] == -6000
        assert Commission.objects.count() == 2

    def test_the_webhook_writes_the_commission(self, client, settings, affiliate, customer):
        settings.ASAAS_WEBHOOK_TOKEN = TOKEN
        settings.RATELIMIT_ENABLE = False
        Referral.objects.create(affiliate=affiliate, organization=customer)

        response = client.post(
            reverse("asaas_webhook"),
            data=_event("PAYMENT_RECEIVED"),
            content_type="application/json",
            headers={"asaas-access-token": TOKEN},
        )

        assert response.status_code == 200
        assert Commission.objects.get().amount_cents == 2000


class TestPayouts:
    def test_the_balance_is_withdrawn_whole_once_it_reaches_the_minimum(self, affiliate, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=100.0)
        with pytest.raises(services.AffiliateError):
            services.request_payout(affiliate, "ana@pix.example")
        _pay(customer, payment_id="pay_2", value=200.0)

        payout = services.request_payout(affiliate, " ana@pix.example ")

        assert (payout.amount_cents, payout.pix_key, payout.status) == (6000, "ana@pix.example", "requested")
        figures = services.numbers(affiliate)
        assert (figures["available_cents"], figures["requested_cents"], figures["can_request"]) == (0, 6000, False)
        with pytest.raises(services.AffiliateError):
            services.request_payout(affiliate, "ana@pix.example")

    def test_a_rejected_withdrawal_goes_back_to_the_balance(self, affiliate, customer, partner):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=300.0)
        payout = services.request_payout(affiliate, "ana@pix.example")

        services.resolve_payout(payout, paid=False, resolved_by=partner, note="Chave inválida")

        assert Payout.objects.get().status == "rejected"
        assert services.numbers(affiliate)["available_cents"] == 6000
        with pytest.raises(services.AffiliateError):
            services.resolve_payout(payout, paid=True, resolved_by=partner)


class TestAccess:
    def test_the_panel_is_only_for_an_affiliate_that_is_on(self, client, affiliate, partner, user):
        client.force_login(user)
        assert client.get(reverse("affiliates:panel")).status_code == 404

        client.force_login(partner)
        assert client.get(reverse("affiliates:panel")).status_code == 200

        Affiliate.objects.update(is_active=False)
        assert client.get(reverse("affiliates:panel")).status_code == 404
        assert client.post(reverse("affiliates:request_payout"), {"pix_key": "x"}).status_code == 404

    def test_the_panel_shows_only_the_affiliate_s_own_referrals(self, client, affiliate, partner, customer):
        other = Affiliate.objects.create(name="Beto", slug="beto", is_active=True)
        Referral.objects.create(affiliate=other, organization=customer)
        mine = Organization.objects.create(name="Loja da Rita")
        Referral.objects.create(affiliate=affiliate, organization=mine)
        client.force_login(partner)

        page = client.get(reverse("affiliates:panel")).content.decode()

        assert "Loja da Rita" in page and "Padaria Sol" not in page

    def test_the_staff_screen_is_closed_to_customers(self, client, user, affiliate):
        client.force_login(user)

        assert client.get(reverse("affiliates_staff:affiliates")).status_code == 403
        assert client.post(reverse("affiliates_staff:toggle", kwargs={"affiliate_id": affiliate.id})).status_code == 403
        assert Affiliate.objects.get().is_active


class TestStaff:
    @pytest.fixture
    def operator(self, client):
        staff = User.objects.create_user(
            email="ops@connektflow.example", password="x", is_staff=True, tos_accepted_at=timezone.now()
        )
        client.force_login(staff)
        return staff

    def test_a_new_affiliate_starts_off_and_is_switched_on(self, client, operator, partner):
        client.post(
            reverse("affiliates_staff:create"),
            {
                "email": "ANA@parceira.example",
                "name": "Ana Souza",
                "slug": "ana-souza",
                "commission_percent": "30",
                "commission_months": "24",
                "min_payout": "100,00",
            },
        )

        affiliate = Affiliate.objects.get()
        assert (affiliate.user, affiliate.slug, affiliate.is_active) == (partner, "ana-souza", False)
        assert (affiliate.commission_percent, affiliate.commission_months, affiliate.min_payout_cents) == (
            30,
            24,
            10000,
        )

        client.post(reverse("affiliates_staff:toggle", kwargs={"affiliate_id": affiliate.id}))
        assert Affiliate.objects.get().is_active
        assert client.get(reverse("affiliates_staff:affiliates")).status_code == 200

    @pytest.mark.parametrize(
        "change",
        [
            {"email": "ninguem@x.example"},
            {"slug": "Ana Souza"},
            {"commission_percent": "0"},
            {"commission_months": "5"},
        ],
    )
    def test_a_form_with_something_wrong_creates_nobody(self, client, operator, partner, change):
        data = {
            "email": partner.email,
            "name": "Ana",
            "slug": "ana",
            "commission_percent": "20",
            "commission_months": "12",
            "min_payout": "50",
        }

        client.post(reverse("affiliates_staff:create"), {**data, **change})

        assert not Affiliate.objects.exists()

    def test_a_requested_withdrawal_is_marked_paid(self, client, operator, affiliate, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=300.0)
        payout = services.request_payout(affiliate, "ana@pix.example")

        client.post(reverse("affiliates_staff:resolve_payout", kwargs={"payout_id": payout.id}), {"action": "paid"})

        payout.refresh_from_db()
        assert (payout.status, payout.resolved_by) == ("paid", operator)
        assert services.numbers(affiliate)["paid_cents"] == 6000
