"""The affiliate programme: who counts as whose referral, what a payment earns, what can be withdrawn."""

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.affiliates import services
from apps.affiliates.models import Affiliate, AffiliateClickDay, Commission, Payout, Referral
from apps.billing import contracting
from apps.billing.models import Coupon, CouponRedemption, OrganizationPlan
from apps.members.models import OrgMembership
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db

TOKEN = "fake-webhook-token"


@pytest.fixture(autouse=True)
def no_hold(monkeypatch):
    """Commissions enter the balance at once, so the tests of everything else need no dates; the hold has its own."""
    monkeypatch.setattr(services, "COMMISSION_HOLD_DAYS", 0)


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


def _receipt():
    return SimpleUploadedFile("comprovante.pdf", b"%PDF-1.4 comprovante", content_type="application/pdf")


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
        organization = OrgMembership.objects.get(user=owner, org_role=OrgMembership.OrgRole.OWNER).organization

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

    def test_an_affiliate_paid_once_earns_on_the_first_payment_only(self, affiliate, customer):
        Affiliate.objects.update(commission_mode=Affiliate.Mode.ONCE)
        Referral.objects.create(affiliate=affiliate, organization=customer)

        _pay(customer, value=1200.0)
        _pay(customer, payment_id="pay_2", paid_on="2026-11-10")
        _pay(customer, payment_id="pay_3", paid_on="2026-12-10")

        commission = Commission.objects.get()
        assert (commission.gateway_payment_id, commission.amount_cents) == ("pay_1", 24000)
        assert Referral.objects.get().commission_until is None

    def test_a_refunded_first_payment_leaves_the_next_one_to_earn_once(self, affiliate, customer):
        Affiliate.objects.update(commission_mode=Affiliate.Mode.ONCE)
        Referral.objects.create(affiliate=affiliate, organization=customer)

        _pay(customer)
        services.record_payment_event(customer, "PAYMENT_REFUNDED", _event("PAYMENT_REFUNDED"))
        _pay(customer, payment_id="pay_2", paid_on="2026-12-10")
        _pay(customer, payment_id="pay_3", paid_on="2027-01-10")

        earned = Commission.objects.filter(status=Commission.Status.EARNED)
        assert list(earned.values_list("gateway_payment_id", flat=True)) == ["pay_2"]

    def test_an_affiliate_moved_to_monthly_earns_for_the_period_from_the_first_payment(self, affiliate, customer):
        Affiliate.objects.update(commission_mode=Affiliate.Mode.ONCE)
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer)

        Affiliate.objects.update(commission_mode=Affiliate.Mode.RECURRING)
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
        services.resolve_payout(payout, paid=True, resolved_by=partner, receipt=_receipt())
        Payout.objects.get().receipt.delete(save=False)

        services.record_payment_event(customer, "PAYMENT_REFUNDED", _event("PAYMENT_REFUNDED"))
        services.record_payment_event(customer, "PAYMENT_REFUNDED", _event("PAYMENT_REFUNDED"))

        assert services.numbers(affiliate)["available_cents"] == -6000
        assert Commission.objects.count() == 2

    def test_a_card_charge_paid_at_checkout_earns_without_waiting_for_the_webhook(self, affiliate, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        payment = {"id": "pay_1", "value": 480.0, "subscription": "sub_1", "status": "CONFIRMED"}

        contracting._record_card_charge(customer, payment)
        _pay(customer, value=480.0)

        commission = Commission.objects.get()
        assert (commission.gateway_payment_id, commission.amount_cents) == ("pay_1", 9600)
        assert Referral.objects.get().first_paid_at is not None

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

    def test_a_commission_is_held_before_it_can_be_withdrawn(self, monkeypatch, affiliate, customer):
        monkeypatch.setattr(services, "COMMISSION_HOLD_DAYS", 15)
        Referral.objects.create(affiliate=affiliate, organization=customer)
        today = timezone.localdate()
        _pay(customer, value=300.0, paid_on=(today - timezone.timedelta(days=20)).isoformat())
        _pay(customer, payment_id="pay_2", value=500.0, paid_on=(today - timezone.timedelta(days=3)).isoformat())

        figures = services.numbers(affiliate)
        assert (figures["available_cents"], figures["held_cents"], figures["commission_cents"]) == (6000, 10000, 16000)

        payout = services.request_payout(affiliate, "ana@pix.example")
        assert payout.amount_cents == 6000
        assert set(payout.commissions.values_list("gateway_payment_id", flat=True)) == {"pay_1"}

    def test_a_held_commission_alone_cannot_be_withdrawn(self, monkeypatch, affiliate, customer):
        monkeypatch.setattr(services, "COMMISSION_HOLD_DAYS", 15)
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=1000.0, paid_on=timezone.localdate().isoformat())

        with pytest.raises(services.AffiliateError):
            services.request_payout(affiliate, "ana@pix.example")
        assert not services.numbers(affiliate)["can_request"]

    def test_a_refund_inside_the_hold_leaves_nothing_to_withdraw(self, monkeypatch, affiliate, customer):
        monkeypatch.setattr(services, "COMMISSION_HOLD_DAYS", 15)
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=1000.0, paid_on=timezone.localdate().isoformat())

        services.record_payment_event(customer, "PAYMENT_REFUNDED", _event("PAYMENT_REFUNDED"))

        figures = services.numbers(affiliate)
        assert (figures["available_cents"], figures["held_cents"]) == (0, 0)

    def test_a_rejected_withdrawal_goes_back_to_the_balance(self, affiliate, customer, partner):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=300.0)
        payout = services.request_payout(affiliate, "ana@pix.example")

        services.resolve_payout(payout, paid=False, resolved_by=partner, note="Chave inválida")

        assert Payout.objects.get().status == "rejected"
        assert services.numbers(affiliate)["available_cents"] == 6000
        with pytest.raises(services.AffiliateError):
            services.resolve_payout(payout, paid=True, resolved_by=partner, receipt=_receipt())


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

    def test_the_panel_s_period_and_status_narrow_what_it_shows(self, client, affiliate, partner, customer):
        old = Referral.objects.create(affiliate=affiliate, organization=customer)
        Referral.objects.filter(pk=old.pk).update(created_at=timezone.now() - timezone.timedelta(days=60))
        Referral.objects.create(affiliate=affiliate, organization=Organization.objects.create(name="Loja Lua"))
        client.force_login(partner)

        everything = client.get(reverse("affiliates:panel")).context
        week = client.get(reverse("affiliates:panel"), {"range": "7"}).context
        paying = client.get(reverse("affiliates:panel"), {"status": "paying"}).context

        assert (everything["numbers"]["signups"], everything["numbers"]["pending"]) == (2, 2)
        assert week["numbers"]["signups"] == 1
        assert [referral.organization.name for referral in week["page_obj"]] == ["Loja Lua"]
        assert list(paying["page_obj"]) == []

    def test_each_tab_lists_only_its_own_rows_a_page_at_a_time(self, client, affiliate, partner, customer):
        for number in range(25):
            Referral.objects.create(
                affiliate=affiliate, organization=Organization.objects.create(name=f"Loja {number}")
            )
        client.force_login(partner)

        first = client.get(reverse("affiliates:panel"), {"tab": "accounts"}).context
        second = client.get(reverse("affiliates:panel"), {"tab": "accounts", "page": "2"}).context
        withdrawals = client.get(reverse("affiliates:panel"), {"tab": "withdrawals"}).context
        unknown = client.get(reverse("affiliates:panel"), {"tab": "../x"}).context

        assert (len(first["page_obj"]), len(second["page_obj"])) == (20, 5)
        assert list(withdrawals["page_obj"]) == []
        assert unknown["tab"] == "accounts"

    def test_the_account_centre_offers_the_panel_only_to_an_affiliate(self, client, affiliate, partner, user):
        panel = reverse("affiliates:panel")

        client.force_login(partner)
        assert f'href="{panel}" class="sidebar-nav-item' in client.get(reverse("accounts:settings")).content.decode()

        client.force_login(user)
        assert (
            f'href="{panel}" class="sidebar-nav-item' not in client.get(reverse("accounts:settings")).content.decode()
        )

    def test_the_staff_screen_is_closed_to_customers(self, client, user, affiliate):
        client.force_login(user)

        assert client.get(reverse("affiliates_staff:affiliates")).status_code == 403
        assert client.post(reverse("affiliates_staff:toggle", kwargs={"affiliate_id": affiliate.id})).status_code == 403
        assert Affiliate.objects.get().is_active


class TestStaff:
    @pytest.fixture
    def operator(self, client):
        staff = User.objects.create_user(
            email="ops@connektflow.example",
            password="x",
            is_staff=True,
            is_superuser=True,
            tos_accepted_at=timezone.now(),
        )
        client.force_login(staff)
        return staff

    def test_a_new_affiliate_starts_on_and_can_be_switched_off(self, client, operator, partner):
        client.post(
            reverse("affiliates_staff:create"),
            {
                "user": str(partner.id),
                "name": "Ana Souza",
                "slug": "ana-souza",
                "commission_percent": "30",
                "commission_months": "24",
                "min_payout": "100,00",
            },
        )

        affiliate = Affiliate.objects.get()
        assert (affiliate.user, affiliate.slug, affiliate.is_active) == (partner, "ana-souza", True)
        assert affiliate.commission_mode == Affiliate.Mode.RECURRING
        (message,) = mail.outbox
        assert message.to == [partner.email]
        assert "/af/ana-souza/" in message.body and "30%" in message.body
        assert (affiliate.commission_percent, affiliate.commission_months, affiliate.min_payout_cents) == (
            30,
            24,
            10000,
        )

        client.post(reverse("affiliates_staff:toggle", kwargs={"affiliate_id": affiliate.id}))
        assert not Affiliate.objects.get().is_active
        assert client.get(reverse("affiliates_staff:affiliates")).status_code == 200

    @pytest.mark.parametrize(
        "change",
        [
            {"user": "00000000-0000-0000-0000-000000000000"},
            {"user": "not-an-id"},
            {"user": "staff"},
            {"commission_mode": "weekly"},
            {"slug": "Ana Souza"},
            {"commission_percent": "0"},
            {"commission_months": "5"},
        ],
    )
    def test_a_form_with_something_wrong_creates_nobody(self, client, operator, partner, change):
        data = {
            "user": str(partner.id),
            "name": "Ana",
            "slug": "ana",
            "commission_percent": "20",
            "commission_months": "12",
            "min_payout": "50",
        }

        if change.get("user") == "staff":
            change = {"user": str(operator.id)}

        client.post(reverse("affiliates_staff:create"), {**data, **change})

        assert not Affiliate.objects.exists()
        assert mail.outbox == []

    @pytest.mark.parametrize(
        "change",
        [{"commission_percent": "25"}, {"commission_mode": "once"}, {"commission_months": "12"}, {"min_payout": "80"}],
    )
    def test_changing_the_terms_e_mails_the_affiliate_the_new_ones(self, client, operator, affiliate, partner, change):
        data = {"name": "Ana", "slug": "ana", "commission_percent": "20", "commission_months": "3", "min_payout": "50"}

        client.post(reverse("affiliates_staff:edit", kwargs={"affiliate_id": affiliate.id}), {**data, **change})

        (message,) = mail.outbox
        assert message.to == [partner.email]
        changed = Affiliate.objects.get()
        assert f"{changed.commission_percent}%" in message.body

    def test_an_edit_that_keeps_the_terms_sends_no_e_mail(self, client, operator, affiliate):
        client.post(
            reverse("affiliates_staff:edit", kwargs={"affiliate_id": affiliate.id}),
            {
                "name": "Ana Souza",
                "slug": "ana-souza",
                "commission_percent": "20",
                "commission_months": "3",
                "min_payout": "50",
                "note": "Parceira antiga",
            },
        )

        assert Affiliate.objects.get().slug == "ana-souza"
        assert mail.outbox == []

    def test_the_operator_is_told_when_the_e_mail_did_not_leave(self, client, operator, partner, settings):
        settings.EMAIL_BACKEND = "apps.common.mail.BudgetedEmailBackend"
        settings.EMAIL_SENDING_ENABLED = False

        response = client.post(
            reverse("affiliates_staff:create"),
            {
                "user": str(partner.id),
                "name": "Ana",
                "slug": "ana",
                "commission_percent": "20",
                "min_payout": "50",
                "commission_mode": "once",
            },
            follow=True,
        )

        assert Affiliate.objects.exists()
        assert "could not be sent" in response.content.decode() or "não pôde ser enviado" in response.content.decode()

    def test_an_affiliate_paid_once_needs_no_period(self, client, operator, partner):
        client.post(
            reverse("affiliates_staff:create"),
            {
                "user": str(partner.id),
                "name": "Ana",
                "slug": "ana",
                "commission_mode": "once",
                "commission_percent": "40",
                "min_payout": "50",
            },
        )

        affiliate = Affiliate.objects.get()
        assert (affiliate.commission_mode, affiliate.commission_percent) == (Affiliate.Mode.ONCE, 40)

    def test_the_accounts_offered_are_customers_who_are_not_affiliates_yet(self, operator, partner, user, affiliate):
        User.objects.create_user(email="ana@outra.example", password="x", name="Ana", tos_accepted_at=timezone.now())

        offered = {row["email"]: row["slug"] for row in services.accounts_to_choose()}

        assert partner.email not in offered and operator.email not in offered
        assert offered["ana@outra.example"] == "ana-2"
        assert user.email in offered

    def test_an_affiliate_that_earned_nothing_is_deleted_with_its_referrals(
        self, client, operator, affiliate, customer
    ):
        Referral.objects.create(affiliate=affiliate, organization=customer)

        client.post(reverse("affiliates_staff:delete", kwargs={"affiliate_id": affiliate.id}))

        assert not Affiliate.objects.exists() and not Referral.objects.exists()
        assert Organization.objects.filter(pk=customer.pk).exists()
        assert User.objects.filter(pk=affiliate.user_id).exists()

    def test_an_affiliate_with_commissions_on_record_is_not_deleted(self, client, operator, affiliate, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer)

        client.post(reverse("affiliates_staff:delete", kwargs={"affiliate_id": affiliate.id}))

        assert Affiliate.objects.exists() and Commission.objects.exists()

    def test_a_requested_withdrawal_is_marked_paid(self, client, operator, affiliate, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=300.0)
        payout = services.request_payout(affiliate, "ana@pix.example")

        client.post(
            reverse("affiliates_staff:resolve_payout", kwargs={"payout_id": payout.id}),
            {"action": "paid", "receipt": _receipt()},
        )

        payout.refresh_from_db()
        assert (payout.status, payout.resolved_by) == ("paid", operator)
        assert services.numbers(affiliate)["paid_cents"] == 6000
        (message,) = mail.outbox
        assert message.to == [affiliate.user.email]
        ((name, content, mimetype),) = message.attachments
        assert (mimetype, content[:5]) == ("application/pdf", b"%PDF-")
        assert (
            client.get(reverse("affiliates_staff:payout_receipt", kwargs={"payout_id": payout.id})).status_code == 200
        )
        payout.receipt.delete(save=False)

    def test_a_requested_withdrawal_is_flagged_on_the_screen_and_in_the_menu(
        self, client, operator, affiliate, customer
    ):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=300.0)
        payout = services.request_payout(affiliate, "ana@pix.example")

        response = client.get(reverse("affiliates_staff:affiliates"))

        assert response.context["open_affiliate_payouts"] == 1
        (row,) = response.context["affiliates"]
        assert row.open_payout == payout
        assert f'data-payout="{payout.id}"' in response.content.decode()

    def test_a_withdrawal_is_not_marked_paid_without_a_valid_receipt(self, client, operator, affiliate, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=300.0)
        payout = services.request_payout(affiliate, "ana@pix.example")
        url = reverse("affiliates_staff:resolve_payout", kwargs={"payout_id": payout.id})

        client.post(url, {"action": "paid"})
        client.post(url, {"action": "paid", "receipt": SimpleUploadedFile("a.pdf", b"<html>x</html>")})

        payout.refresh_from_db()
        assert payout.status == "requested" and not payout.receipt
        assert mail.outbox == []

    def test_asking_for_a_withdrawal_e_mails_the_affiliate_and_the_superadmins(
        self, client, operator, affiliate, partner, customer
    ):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=300.0)
        client.force_login(partner)

        client.post(reverse("affiliates:request_payout"), {"pix_key": "ana@pix.example"})

        payout = Payout.objects.get()
        to_affiliate, to_team = mail.outbox
        assert to_affiliate.to == [partner.email]
        assert "60,00" in to_affiliate.body and "ana@pix.example" in to_affiliate.body
        assert services.payout_deadline(payout).strftime("%d/%m/%Y") in to_affiliate.body
        assert to_team.to == [operator.email]

    def test_an_affiliate_sees_only_their_own_receipts(self, client, affiliate, partner, user, customer):
        Referral.objects.create(affiliate=affiliate, organization=customer)
        _pay(customer, value=300.0)
        payout = services.request_payout(affiliate, "ana@pix.example")
        services.resolve_payout(payout, paid=True, resolved_by=None, receipt=_receipt())
        url = reverse("affiliates:payout_receipt", kwargs={"payout_id": payout.id})
        other = Affiliate.objects.create(user=user, name="Bia", slug="bia", is_active=True)

        client.force_login(partner)
        assert client.get(url).status_code == 200
        client.force_login(other.user)
        assert client.get(url).status_code == 404
        Payout.objects.get().receipt.delete(save=False)
