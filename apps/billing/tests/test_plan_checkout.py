"""The organization plan: brands bought together at checkout and paid by card.

The gateway is a stub, as in test_contracting. What matters here is the
price charged, that a declined card leaves nothing owed, that the card ends
up on the subscription, and which workspaces the plan opens.
"""

from __future__ import annotations

import pytest
from django.db.models.signals import post_save
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.models import User
from apps.accounts.signals import create_organization_on_user_create
from apps.billing import contracting, services
from apps.billing.gateways.asaas import AsaasAPIError
from apps.billing.models import OrganizationPlan, WorkspaceSubscription
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import PlatformVisibility
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

ACTIVE = WorkspaceSubscription.Status.ACTIVE
PENDING = WorkspaceSubscription.Status.PENDING
CARD = {"holderName": "Ana", "number": "5162306219378829", "expiryMonth": "05", "expiryYear": "2030", "ccv": "318"}
HOLDER = {"name": "Ana", "email": "ana@example.com", "cpfCnpj": "24971563792", "postalCode": "89010000"}
PRICES = {
    "BILLING_WORKSPACE_PRICE_CENTS": 5000,
    "BILLING_ANNUAL_DISCOUNT_PERCENT": 20,
    "BILLING_VOLUME_DISCOUNTS": [(5, 10), (10, 15)],
}


@pytest.fixture(autouse=True)
def _prices():
    services.invalidate_price_cache()
    with override_settings(**PRICES):
        yield
    services.invalidate_price_cache()


class StubAsaas:
    """The happy path of Asaas, recording what it was asked."""

    def __init__(self, *, refuse_card=False, payments=None):
        self.refuse_card = refuse_card
        self.payments = payments
        self.subscriptions = []
        self.charged = []
        self.cards_kept = []
        self.canceled = []
        self.values = []

    def find_customer_by_reference(self, external_reference):
        return None

    def create_customer(self, **kwargs):
        return {"id": "cus_1"}

    def create_subscription(self, *, value_cents, cycle, **_kwargs):
        self.subscriptions.append({"value_cents": value_cents, "cycle": cycle})
        return {"id": f"sub_{len(self.subscriptions)}"}

    def list_subscription_payments(self, subscription_id):
        if self.payments is not None:
            return self.payments
        return [{"id": "pay_1", "status": "PENDING", "dueDate": "2026-11-18"}]

    def pay_with_card(self, payment_id, *, card, holder, remote_ip):
        if self.refuse_card:
            raise AsaasAPIError(
                "Asaas returned HTTP 400.",
                status_code=400,
                errors=[{"code": "invalid_creditCard", "description": "Transação não autorizada."}],
            )
        self.charged.append((payment_id, card["number"], remote_ip))
        return {
            "status": "CONFIRMED",
            "creditCard": {"creditCardNumber": "8829", "creditCardBrand": "MASTERCARD", "creditCardToken": "tok_1"},
        }

    def set_subscription_card(self, subscription_id, *, card_token, remote_ip):
        self.cards_kept.append((subscription_id, card_token))
        return {"billingType": "CREDIT_CARD"}

    def cancel_subscription(self, subscription_id):
        self.canceled.append(subscription_id)
        return {"deleted": True}

    def update_payment_value(self, payment_id, value_cents):
        self.first_values = [*getattr(self, "first_values", []), (payment_id, value_cents)]
        return {}

    def update_subscription_value(self, subscription_id, value_cents):
        self.values.append((subscription_id, value_cents))
        return {}


def _org_with_workspaces(count, name="Acme"):
    org = Organization.objects.create(name=name)
    workspaces = [Workspace.objects.create(organization=org, name=f"Brand {n}") for n in range(1, count + 1)]
    return org, workspaces


def _status(workspace):
    return services.ensure_subscription(workspace).status


def _buy(org, brands=1, cycle=WorkspaceSubscription.Cycle.MONTHLY, client=None):
    return contracting.purchase_plan(
        org, brands=brands, cycle=cycle, card=CARD, holder=HOLDER, remote_ip="177.0.0.1", client=client
    )


# -- price -------------------------------------------------------------------


def test_each_brand_is_priced_at_its_volume_tier_and_yearly_comes_on_top():
    assert services.quote_plan(1)["monthly_cents"] == 5000
    # Four at list price, the 5th at 10% off.
    assert services.quote_plan(5)["monthly_cents"] == 4 * 5000 + 4500
    # Nine as above plus the 10th at 15% off.
    assert services.quote_plan(10)["monthly_cents"] == 4 * 5000 + 5 * 4500 + 4250
    yearly = services.quote_plan(1, WorkspaceSubscription.Cycle.YEARLY)
    assert (yearly["monthly_cents"], yearly["charge_cents"], yearly["yearly_saving_cents"]) == (4000, 48000, 12000)


# -- purchase ------------------------------------------------------------------


def test_the_first_charge_is_paid_by_card_and_the_card_stays_on_the_subscription():
    org, workspaces = _org_with_workspaces(3)
    stub = StubAsaas()

    plan = _buy(org, brands=2, cycle=WorkspaceSubscription.Cycle.YEARLY, client=stub)

    assert stub.subscriptions == [{"value_cents": 2 * 4000 * 12, "cycle": "YEARLY"}]
    assert stub.charged == [("pay_1", CARD["number"], "177.0.0.1")]
    assert stub.cards_kept == [("sub_1", "tok_1")]
    assert plan.status == ACTIVE and plan.brands == 2 and plan.price_cents == 8000
    assert (plan.card_brand, plan.card_last4) == ("MASTERCARD", "8829")
    # The two oldest brands open; the third waits for a brand of its own.
    assert [_status(w) for w in workspaces] == [ACTIVE, ACTIVE, PENDING]


def test_a_declined_card_cancels_the_subscription_and_opens_nothing():
    org, workspaces = _org_with_workspaces(1)
    stub = StubAsaas(refuse_card=True)

    with pytest.raises(contracting.CardRefusedError, match="Transação não autorizada"):
        _buy(org, client=stub)

    plan = OrganizationPlan.objects.get(organization=org)
    assert stub.canceled == ["sub_1"]
    assert plan.status == PENDING and plan.gateway_subscription_id == ""
    assert _status(workspaces[0]) == PENDING


def test_a_charge_that_went_through_after_a_timeout_is_not_charged_again():
    org, workspaces = _org_with_workspaces(1)
    OrganizationPlan.objects.create(organization=org, brands=1, gateway_subscription_id="sub_old")
    stub = StubAsaas(payments=[{"id": "pay_old", "status": "CONFIRMED"}])

    plan = _buy(org, client=stub)

    assert plan.status == ACTIVE
    assert stub.charged == [] and stub.subscriptions == [] and stub.canceled == []
    assert _status(workspaces[0]) == ACTIVE


# -- coverage ----------------------------------------------------------------


def test_adding_brands_charges_more_from_the_next_charge_and_opens_the_next_workspace():
    org, workspaces = _org_with_workspaces(2)
    stub = StubAsaas()
    plan = _buy(org, brands=1, client=stub)
    assert _status(workspaces[1]) == PENDING

    contracting.change_plan_brands(plan, 2, client=stub)

    assert stub.values == [("sub_1", 10000)]
    assert _status(workspaces[1]) == ACTIVE


def test_a_workspace_paying_on_its_own_or_comped_does_not_take_a_brand():
    org, (own, comped, fresh) = _org_with_workspaces(3)
    services.ensure_subscription(own)
    WorkspaceSubscription.objects.filter(workspace=own).update(gateway_subscription_id="sub_own", status=ACTIVE)
    services.grant_courtesy(comped)

    _buy(org, brands=1, client=StubAsaas())

    assert WorkspaceSubscription.objects.get(workspace=fresh).plan is not None
    assert WorkspaceSubscription.objects.get(workspace=own).plan is None


@override_settings(ASAAS_WEBHOOK_TOKEN="tok", RATELIMIT_ENABLE=False)
def test_the_gateway_events_of_the_plan_reach_the_workspaces_it_covers(client):
    org, workspaces = _org_with_workspaces(1)
    _buy(org, client=StubAsaas())

    def post(event, event_id):
        payload = {"id": event_id, "event": event, "payment": {"id": "pay_2", "subscription": "sub_1"}}
        return client.post(
            reverse("asaas_webhook"),
            data=payload,
            content_type="application/json",
            headers={"asaas-access-token": "tok"},
        )

    assert post("PAYMENT_OVERDUE", "evt_1").status_code == 200
    assert _status(workspaces[0]) == WorkspaceSubscription.Status.PAST_DUE
    post("PAYMENT_CONFIRMED", "evt_2")
    assert _status(workspaces[0]) == ACTIVE


def test_deleting_the_organization_cancels_the_plan_subscription():
    org, _workspaces = _org_with_workspaces(1)
    _buy(org, client=StubAsaas())
    stub = StubAsaas()

    contracting.cancel_organization_subscriptions(org, client=stub)

    assert stub.canceled == ["sub_1"]


# -- screens -------------------------------------------------------------------


@pytest.fixture
def owner_of():
    def make(org, *, role=OrgMembership.OrgRole.OWNER, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER):
        post_save.disconnect(create_organization_on_user_create, sender=User)
        try:
            user = User.objects.create_user(
                email=f"{role}-{workspace_role}@example.com", password="x", tos_accepted_at=timezone.now()
            )
        finally:
            post_save.connect(create_organization_on_user_create, sender=User)
        OrgMembership.objects.create(user=user, organization=org, org_role=role)
        for workspace in Workspace.objects.filter(organization=org):
            WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=workspace_role)
        return user

    return make


def test_only_who_manages_billing_reaches_the_checkout(client, owner_of):
    org, (workspace,) = _org_with_workspaces(1)
    viewer = owner_of(
        org, role=OrgMembership.OrgRole.MEMBER, workspace_role=WorkspaceMembership.WorkspaceRole.CONTRIBUTOR
    )
    client.force_login(viewer)

    assert client.get(reverse("billing:checkout", kwargs={"workspace_id": workspace.id})).status_code == 403
    response = client.post(reverse("billing:checkout_pay", kwargs={"workspace_id": workspace.id}), {"brands": "1"})
    assert response.status_code == 403
    assert not OrganizationPlan.objects.filter(organization=org).exists()


CARD_FORM = {
    "cycle": "monthly",
    "card_number": "5162 3062 1937 8829",
    "card_holder": "Ana Souza",
    "card_expiry": "05/30",
    "card_ccv": "318",
    "postal_code": "89010-000",
    "address_number": "10",
}


@override_settings(RATELIMIT_ENABLE=False)
def test_paying_at_checkout_opens_the_brands_chosen_and_keeps_no_card(client, owner_of, monkeypatch):
    org, (ticked, left_out) = _org_with_workspaces(2)
    services.set_payer_identity(org, tax_id="24971563792", phone="47988776655")
    owner = owner_of(org)
    client.force_login(owner)
    stub = StubAsaas()
    monkeypatch.setattr(contracting, "AsaasClient", lambda: stub)

    response = client.post(
        reverse("billing:checkout_pay", kwargs={"workspace_id": ticked.id}),
        {**CARD_FORM, "workspaces": [str(ticked.id)], "new_brand": ["Loja Bela", " "], f"name_{ticked.id}": "Sol Café"},
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("billing:checkout_welcome", kwargs={"workspace_id": ticked.id})
    assert stub.subscriptions == [{"value_cents": 10000, "cycle": "MONTHLY"}]
    assert stub.charged[0][1] == "5162306219378829"
    created = Workspace.objects.get(organization=org, name="Loja Bela")
    assert WorkspaceMembership.objects.filter(user=owner, workspace=created).exists()
    assert [_status(w) for w in (ticked, created, left_out)] == [ACTIVE, ACTIVE, PENDING]
    ticked.refresh_from_db()
    assert ticked.name == "Sol Café"
    plan = OrganizationPlan.objects.get(organization=org)
    assert "5162306219378829" not in str(list(OrganizationPlan.objects.filter(pk=plan.pk).values()))


@override_settings(RATELIMIT_ENABLE=False)
def test_a_brand_of_another_organization_cannot_be_put_on_the_plan(client, owner_of, monkeypatch):
    org, (own,) = _org_with_workspaces(1)
    _other, (foreign,) = _org_with_workspaces(1, name="Other")
    services.set_payer_identity(org, tax_id="24971563792", phone="47988776655")
    client.force_login(owner_of(org))
    stub = StubAsaas()
    monkeypatch.setattr(contracting, "AsaasClient", lambda: stub)

    client.post(
        reverse("billing:checkout_pay", kwargs={"workspace_id": own.id}),
        {**CARD_FORM, "workspaces": [str(own.id), str(foreign.id)], f"name_{foreign.id}": "Hijacked"},
    )

    assert stub.subscriptions == [{"value_cents": 5000, "cycle": "MONTHLY"}]
    foreign.refresh_from_db()
    assert foreign.name == "Brand 1" and _status(foreign) == PENDING


@override_settings(RATELIMIT_ENABLE=False)
def test_a_refused_card_answers_in_place_for_the_page_to_show(client, owner_of, monkeypatch):
    org, (workspace,) = _org_with_workspaces(1)
    services.set_payer_identity(org, tax_id="24971563792", phone="47988776655")
    client.force_login(owner_of(org))
    monkeypatch.setattr(contracting, "AsaasClient", lambda: StubAsaas(refuse_card=True))

    response = client.post(
        reverse("billing:checkout_pay", kwargs={"workspace_id": workspace.id}),
        {**CARD_FORM, "workspaces": [str(workspace.id)]},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 422
    assert "Transação não autorizada" in response.content.decode()


def test_the_quote_prices_the_brands_ticked_and_added(client, owner_of):
    org, (workspace,) = _org_with_workspaces(1)
    client.force_login(owner_of(org))

    response = client.get(
        reverse("billing:checkout_quote", kwargs={"workspace_id": workspace.id}),
        {"workspaces": [str(workspace.id)], "new_brand": ["A", "B", "C", "D"], "cycle": "yearly"},
    )

    assert response.context["quote"]["brands"] == 5
    assert response.context["quote"]["charge_cents"] == (4 * 4000 + 3600) * 12


def test_the_welcome_page_leads_to_the_first_steps(client, owner_of):
    org, (workspace,) = _org_with_workspaces(1)
    client.force_login(owner_of(org))

    html = client.get(reverse("billing:checkout_welcome", kwargs={"workspace_id": workspace.id})).content.decode()

    for name in ("social_accounts:list", "composer:create_landing", "automations:list"):
        assert reverse(name, kwargs={"workspace_id": workspace.id}) in html


def test_a_new_workspace_takes_a_free_brand_of_the_plan_on_its_contracting_page(client, owner_of):
    org, (first,) = _org_with_workspaces(1)
    _buy(org, brands=2, client=StubAsaas())
    second = Workspace.objects.create(organization=org, name="Brand 2")
    user = owner_of(org)
    WorkspaceMembership.objects.get_or_create(user=user, workspace=second)
    client.force_login(user)

    response = client.get(reverse("billing:subscribe_modal", kwargs={"workspace_id": second.id}))

    assert response.status_code == 302
    assert _status(second) == ACTIVE


def test_the_checkout_lists_each_visible_network_once_and_never_a_hidden_one(client, owner_of):
    org, (workspace,) = _org_with_workspaces(1)
    client.force_login(owner_of(org))
    PlatformVisibility.set_visible("pinterest", False)

    networks = client.get(reverse("billing:checkout", kwargs={"workspace_id": workspace.id})).context["networks"]

    keys = [key for key, _name in networks]
    assert "pinterest" not in keys
    assert keys.count("linkedin") == 1 and "linkedin_company" not in keys
    assert "instagram_login" not in keys and "instagram" in keys


def test_a_first_purchase_of_one_brand_has_nothing_to_tick(client, owner_of):
    org, (workspace,) = _org_with_workspaces(1)
    client.force_login(owner_of(org))

    response = client.get(reverse("billing:checkout", kwargs={"workspace_id": workspace.id}))

    assert response.context["choose_brands"] is False
    # The brand is still on the order, ticked out of sight.
    page = response.content.decode()
    assert f'name="workspaces" value="{workspace.pk}" data-quote checked class="hidden"' in page
    # A brand that has not been used yet is not called inactive.
    with translation.override("pt-br"):
        assert translation.gettext("Activated on payment") in page
        assert translation.gettext("Inactive") not in page


def test_several_brands_or_an_earlier_plan_let_the_brands_be_ticked(client, owner_of):
    org, workspaces = _org_with_workspaces(2)
    client.force_login(owner_of(org))
    url = reverse("billing:checkout", kwargs={"workspace_id": workspaces[0].id})
    assert client.get(url).context["choose_brands"] is True

    renewing, (brand,) = _org_with_workspaces(1, name="Renewing")
    _buy(renewing, client=StubAsaas())
    OrganizationPlan.objects.filter(organization=renewing).update(status=WorkspaceSubscription.Status.CANCELED)
    client.force_login(owner_of(renewing, role=OrgMembership.OrgRole.ADMIN))
    assert client.get(reverse("billing:checkout", kwargs={"workspace_id": brand.id})).context["choose_brands"] is True


@override_settings(RATELIMIT_ENABLE=False)
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("card_expiry", "13/30", "Enter the card's expiry as MM/YY."),
        ("card_expiry", "01/20", "This card has expired."),
        ("card_number", "1234 5678 9101 1121", "This card number is not valid. Check the digits."),
    ],
)
def test_a_card_that_cannot_be_charged_is_refused_before_asaas_is_called(
    client, owner_of, monkeypatch, field, value, message
):
    org, (workspace,) = _org_with_workspaces(1)
    services.set_payer_identity(org, tax_id="24971563792", phone="47988776655")
    client.force_login(owner_of(org))
    monkeypatch.setattr(contracting, "AsaasClient", lambda: pytest.fail("Asaas must not be called"))

    response = client.post(
        reverse("billing:checkout_pay", kwargs={"workspace_id": workspace.id}),
        {**CARD_FORM, field: value, "workspaces": [str(workspace.id)]},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 422
    with translation.override("pt-br"):
        assert translation.gettext(message) in response.content.decode()
    assert not OrganizationPlan.objects.filter(organization=org).exists()
