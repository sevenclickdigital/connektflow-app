"""Brands added to an active plan are charged for the days left until its next charge.

The gateway is a stub. What is protected: the proportional value, that the
brands only go on the plan once that value is paid, that a declined card
changes nothing, and that a payment is never applied twice.
"""

from __future__ import annotations

import datetime

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.billing import contracting, services
from apps.billing.gateways.asaas import AsaasAPIError, AsaasError
from apps.billing.models import PlanBrandAddition, WorkspaceSubscription
from apps.billing.tests.test_plan_checkout import ACTIVE, PENDING, PRICES, _buy, _org_with_workspaces, _status
from apps.billing.tests.test_plan_checkout import StubAsaas as PlanStub
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 11, 25)
NEXT_CHARGE = "2026-12-10"
STATUS = PlanBrandAddition.Status


@pytest.fixture(autouse=True)
def _prices_and_today(monkeypatch):
    services.invalidate_price_cache()
    cache.clear()
    monkeypatch.setattr(timezone, "localdate", lambda *args, **kwargs: TODAY)
    with override_settings(**PRICES, ASAAS_WEBHOOK_TOKEN="tok", RATELIMIT_ENABLE=False):
        yield
    services.invalidate_price_cache()


class StubAsaas(PlanStub):
    """The plan's stub, plus one-off charges and the subscription's next charge."""

    def __init__(self, *, refuse_token=False, charge_status="CONFIRMED", **kwargs):
        super().__init__(**kwargs)
        self.refuse_token = refuse_token
        self.charge_status = charge_status
        self.one_offs = []
        self.deleted = []
        self.found = {}

    def get_subscription(self, subscription_id):
        return {"id": subscription_id, "nextDueDate": NEXT_CHARGE}

    def create_payment(self, *, customer_id, value_cents, external_reference, card_token="", remote_ip="", **_kwargs):
        if card_token and self.refuse_token:
            raise AsaasAPIError(
                "Asaas returned HTTP 400.",
                status_code=400,
                errors=[{"code": "invalid_creditCard", "description": "Transação não autorizada."}],
            )
        number = len(self.one_offs) + 1
        self.one_offs.append(
            {"value_cents": value_cents, "card_token": card_token, "remote_ip": remote_ip, "ref": external_reference}
        )
        return {
            "id": f"pay_extra_{number}",
            "status": self.charge_status if card_token else "PENDING",
            "invoiceUrl": f"https://asaas.example/i/{number}",
        }

    def delete_payment(self, payment_id):
        self.deleted.append(payment_id)
        return {"deleted": True}

    def find_payment_by_reference(self, external_reference):
        """What the gateway holds for a reference: ``self.found`` when a test sets it."""
        if external_reference in self.found:
            return self.found[external_reference]
        for number, charge in enumerate(self.one_offs, start=1):
            if charge["ref"] == external_reference:
                return {"id": f"pay_extra_{number}", "status": "PENDING"}
        return None


def _plan_with_two_brands(*, token=True, cycle=WorkspaceSubscription.Cycle.MONTHLY):
    """An active plan of one brand in an organization of two, its next charge on NEXT_CHARGE."""
    org, workspaces = _org_with_workspaces(2)
    stub = StubAsaas()
    plan = _buy(org, brands=1, cycle=cycle, client=stub)
    # The first charge is paid: the gateway holds no unpaid one.
    stub.payments = []
    if not token:
        plan.gateway_card_token = None
        plan.save(update_fields=["gateway_card_token"])
    return plan, workspaces, stub


def _paid(client, payment_id, event_id="evt_1", event="PAYMENT_CONFIRMED"):
    return client.post(
        reverse("asaas_webhook"),
        data={"id": event_id, "event": event, "payment": {"id": payment_id, "subscription": None}},
        content_type="application/json",
        headers={"asaas-access-token": "tok"},
    )


class TestTheProportionalValue:
    def test_half_a_month_left_is_half_the_added_brand(self):
        plan, _workspaces, _stub = _plan_with_two_brands()

        quote = services.quote_brand_addition(plan, 2, datetime.date(2026, 12, 10), today=TODAY)

        assert (quote["cycle_days"], quote["days_left"]) == (30, 15)
        assert quote["amount_cents"] == 2500
        assert quote["new_charge_cents"] == 10000

    def test_a_yearly_plan_charges_the_months_left_of_the_year(self):
        """Bought in January, a brand added in March pays until next January."""
        plan, _workspaces, _stub = _plan_with_two_brands(cycle=WorkspaceSubscription.Cycle.YEARLY)

        quote = services.quote_brand_addition(plan, 2, datetime.date(2027, 1, 10), today=datetime.date(2026, 3, 10))

        assert (quote["cycle_days"], quote["days_left"]) == (365, 306)
        assert quote["added_charge_cents"] == 48000
        assert quote["amount_cents"] == 40241

    def test_a_value_under_the_gateway_s_minimum_is_not_charged(self):
        plan, _workspaces, _stub = _plan_with_two_brands()

        quote = services.quote_brand_addition(plan, 2, datetime.date(2026, 11, 27), today=TODAY)

        assert quote["days_left"] == 2
        assert quote["amount_cents"] == 0

    def test_nothing_is_charged_for_no_brand_added_or_a_charge_due_today(self):
        plan, _workspaces, _stub = _plan_with_two_brands()

        assert services.quote_brand_addition(plan, 1, datetime.date(2026, 12, 10), today=TODAY)["amount_cents"] == 0
        assert services.quote_brand_addition(plan, 2, TODAY, today=TODAY)["amount_cents"] == 0

    def test_the_charge_already_generated_is_the_next_one(self):
        plan, _workspaces, stub = _plan_with_two_brands()
        stub.payments = [{"id": "pay_2", "status": "PENDING", "dueDate": "2026-12-05"}]

        assert contracting.next_charge_date(plan, stub) == datetime.date(2026, 12, 5)


class TestACardOnFile:
    def test_buying_a_plan_keeps_the_card_s_token_and_never_the_card(self):
        plan, _workspaces, _stub = _plan_with_two_brands()

        plan.refresh_from_db()
        assert plan.gateway_card_token == "tok_1"
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM billing_organization_plan WHERE id = %s", [plan.pk])
            stored = str(cursor.fetchone())
        assert "5162306219378829" not in stored
        # Encrypted at rest.
        assert "tok_1" not in stored

    def test_the_card_is_charged_now_and_the_brand_goes_on_the_plan(self):
        plan, workspaces, stub = _plan_with_two_brands()

        addition, quote = contracting.add_plan_brands(
            plan, 2, workspaces=[workspaces[1]], remote_ip="177.0.0.1", client=stub
        )

        assert addition is None
        assert quote["amount_cents"] == 2500
        assert [(c["value_cents"], c["card_token"], c["remote_ip"]) for c in stub.one_offs] == [
            (2500, "tok_1", "177.0.0.1")
        ]
        assert stub.values == [("sub_1", 10000)]
        assert _status(workspaces[1]) == ACTIVE
        assert PlanBrandAddition.objects.get(plan=plan).status == STATUS.APPLIED

    def test_a_declined_card_changes_nothing(self):
        plan, workspaces, stub = _plan_with_two_brands()
        stub.refuse_token = True

        with pytest.raises(contracting.CardRefusedError):
            contracting.add_plan_brands(plan, 2, workspaces=[workspaces[1]], remote_ip="177.0.0.1", client=stub)

        plan.refresh_from_db()
        assert plan.brands == 1
        assert stub.values == []
        assert _status(workspaces[1]) == PENDING
        assert PlanBrandAddition.objects.get(plan=plan).status == STATUS.CANCELED

    def test_a_charge_under_analysis_waits_for_the_gateway(self):
        plan, workspaces, stub = _plan_with_two_brands()
        stub.charge_status = "PENDING"

        addition, _quote = contracting.add_plan_brands(
            plan, 2, workspaces=[workspaces[1]], remote_ip="177.0.0.1", client=stub
        )

        assert addition.status == STATUS.PENDING
        assert _status(workspaces[1]) == PENDING

    def test_a_charge_whose_answer_was_lost_is_not_taken_twice(self):
        """The first attempt timed out but the card was charged: the retry applies it."""
        plan, workspaces, stub = _plan_with_two_brands()
        lost = PlanBrandAddition.objects.create(
            plan=plan, brands=2, workspace_ids=[str(workspaces[1].pk)], amount_cents=2500
        )
        stub.found[f"plan-brands:{lost.pk}"] = {"id": "pay_lost", "status": "CONFIRMED"}

        addition, _quote = contracting.add_plan_brands(
            plan, 2, workspaces=[workspaces[1]], remote_ip="177.0.0.1", client=stub
        )

        lost.refresh_from_db()
        assert addition is None
        assert stub.one_offs == []
        assert lost.status == STATUS.APPLIED and lost.gateway_payment_id == "pay_lost"
        assert stub.values == [("sub_1", 10000)]
        assert _status(workspaces[1]) == ACTIVE

    def test_a_payment_page_that_is_not_https_is_not_kept(self):
        plan, workspaces, stub = _plan_with_two_brands(token=False)
        stub.create_payment = lambda **kwargs: {"id": "pay_x", "status": "PENDING", "invoiceUrl": "javascript:alert(1)"}

        with pytest.raises(AsaasError):
            contracting.add_plan_brands(plan, 2, workspaces=[workspaces[1]], client=stub)

        assert PlanBrandAddition.objects.get(plan=plan).invoice_url == ""

    def test_a_payment_for_a_plan_no_longer_active_changes_nothing(self):
        plan, workspaces, stub = _plan_with_two_brands(token=False)
        addition, _quote = contracting.add_plan_brands(plan, 2, workspaces=[workspaces[1]], client=stub)
        plan.status = WorkspaceSubscription.Status.CANCELED
        plan.save(update_fields=["status"])

        contracting.apply_brand_addition(addition, client=stub)

        addition.refresh_from_db()
        assert addition.status == STATUS.PENDING
        assert stub.values == []

    def test_a_value_under_the_minimum_adds_the_brand_with_no_charge(self):
        plan, workspaces, stub = _plan_with_two_brands()
        stub.payments = [{"id": "pay_2", "status": "PENDING", "dueDate": "2026-11-27"}]

        addition, quote = contracting.add_plan_brands(plan, 2, workspaces=[workspaces[1]], client=stub)

        assert addition is None and quote["amount_cents"] == 0
        assert stub.one_offs == []
        assert _status(workspaces[1]) == ACTIVE
        assert not PlanBrandAddition.objects.exists()


class TestWithoutACardOnFile:
    """A plan paid by Pix, or by a card whose token was not kept."""

    def test_a_charge_is_created_and_nothing_changes_until_it_is_paid(self):
        plan, workspaces, stub = _plan_with_two_brands(token=False)

        addition, _quote = contracting.add_plan_brands(
            plan, 3, workspaces=[workspaces[1]], new_brand_names=["Loja Nova"], client=stub
        )

        assert addition.status == STATUS.PENDING
        assert addition.invoice_url == "https://asaas.example/i/1"
        assert addition.amount_cents == 5000
        assert stub.one_offs[0]["card_token"] == ""
        plan.refresh_from_db()
        assert plan.brands == 1
        assert stub.values == []
        assert _status(workspaces[1]) == PENDING
        assert not Workspace.objects.filter(name="Loja Nova").exists()

    def test_the_payment_puts_the_brands_on_the_plan(self, client, monkeypatch, user):
        plan, workspaces, stub = _plan_with_two_brands(token=False)
        monkeypatch.setattr(contracting, "AsaasClient", lambda: stub)
        addition, _quote = contracting.add_plan_brands(
            plan, 3, workspaces=[workspaces[1]], new_brand_names=["Loja Nova"], owner=user, client=stub
        )

        assert _paid(client, addition.gateway_payment_id).status_code == 200

        plan.refresh_from_db()
        addition.refresh_from_db()
        assert plan.brands == 3
        assert addition.status == STATUS.APPLIED
        assert stub.values == [("sub_1", 15000)]
        assert _status(workspaces[1]) == ACTIVE
        assert _status(Workspace.objects.get(organization=plan.organization, name="Loja Nova")) == ACTIVE

    def test_the_same_payment_reported_twice_is_applied_once(self, client, monkeypatch, user):
        plan, workspaces, stub = _plan_with_two_brands(token=False)
        monkeypatch.setattr(contracting, "AsaasClient", lambda: stub)
        addition, _quote = contracting.add_plan_brands(plan, 2, new_brand_names=["Loja Nova"], owner=user, client=stub)

        _paid(client, addition.gateway_payment_id, "evt_1")
        _paid(client, addition.gateway_payment_id, "evt_2", "PAYMENT_RECEIVED")

        assert stub.values == [("sub_1", 10000)]
        assert Workspace.objects.filter(organization=plan.organization, name="Loja Nova").count() == 1

    def test_an_unpaid_charge_event_leaves_the_request_waiting(self, client, monkeypatch):
        plan, workspaces, stub = _plan_with_two_brands(token=False)
        monkeypatch.setattr(contracting, "AsaasClient", lambda: stub)
        addition, _quote = contracting.add_plan_brands(plan, 2, workspaces=[workspaces[1]], client=stub)

        _paid(client, addition.gateway_payment_id, event="PAYMENT_OVERDUE")

        addition.refresh_from_db()
        plan.refresh_from_db()
        assert addition.status == STATUS.PENDING
        assert plan.status == ACTIVE and plan.brands == 1

    def test_a_new_request_withdraws_the_one_before(self):
        plan, workspaces, stub = _plan_with_two_brands(token=False)
        first, _quote = contracting.add_plan_brands(plan, 2, workspaces=[workspaces[1]], client=stub)

        second, _quote = contracting.add_plan_brands(plan, 2, workspaces=[workspaces[1]], client=stub)

        first.refresh_from_db()
        assert stub.deleted == [first.gateway_payment_id]
        assert first.status == STATUS.CANCELED
        assert second.status == STATUS.PENDING

    def test_a_payment_of_someone_else_s_charge_is_ignored(self, client):
        plan, _workspaces, _stub = _plan_with_two_brands(token=False)

        assert _paid(client, "pay_unknown").status_code == 200

        plan.refresh_from_db()
        assert plan.brands == 1


class TestAtTheCheckout:
    def test_the_running_total_says_what_is_charged_now(self, client, owner_of, monkeypatch):
        plan, workspaces, stub = _plan_with_two_brands()
        client.force_login(owner_of(plan.organization))
        monkeypatch.setattr("apps.billing.views.AsaasClient", lambda: stub)

        response = client.get(
            reverse("billing:checkout_quote", kwargs={"workspace_id": workspaces[0].id}),
            {"workspaces": [str(workspaces[1].id)]},
        )

        assert response.context["addition"]["amount_cents"] == 2500

    def test_without_a_card_the_person_is_sent_to_pay_and_the_page_keeps_the_request(
        self, client, owner_of, monkeypatch
    ):
        plan, workspaces, stub = _plan_with_two_brands(token=False)
        client.force_login(owner_of(plan.organization))
        monkeypatch.setattr(contracting, "AsaasClient", lambda: stub)

        response = client.post(
            reverse("billing:checkout_pay", kwargs={"workspace_id": workspaces[0].id}),
            {"workspaces": [str(workspaces[1].id)]},
        )

        assert response.status_code == 302
        assert response["Location"] == "https://asaas.example/i/1"
        assert _status(workspaces[1]) == PENDING
        page = client.get(reverse("billing:checkout", kwargs={"workspace_id": workspaces[0].id}))
        assert page.context["pending_addition"].amount_cents == 2500

    def test_with_a_card_the_brand_is_added_at_once(self, client, owner_of, monkeypatch):
        plan, workspaces, stub = _plan_with_two_brands()
        client.force_login(owner_of(plan.organization))
        monkeypatch.setattr(contracting, "AsaasClient", lambda: stub)

        response = client.post(
            reverse("billing:checkout_pay", kwargs={"workspace_id": workspaces[0].id}),
            {"workspaces": [str(workspaces[1].id)]},
        )

        assert response["Location"] == reverse("calendar:calendar", kwargs={"workspace_id": workspaces[0].id})
        assert _status(workspaces[1]) == ACTIVE
        assert stub.one_offs[0]["value_cents"] == 2500

    def test_a_declined_card_answers_in_place(self, client, owner_of, monkeypatch):
        plan, workspaces, stub = _plan_with_two_brands()
        stub.refuse_token = True
        client.force_login(owner_of(plan.organization))
        monkeypatch.setattr(contracting, "AsaasClient", lambda: stub)

        response = client.post(
            reverse("billing:checkout_pay", kwargs={"workspace_id": workspaces[0].id}),
            {"workspaces": [str(workspaces[1].id)]},
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 422
        assert _status(workspaces[1]) == PENDING
