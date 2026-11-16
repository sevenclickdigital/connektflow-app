"""Account centre → Billing: the brands whose billing the user manages, only."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

ROLE = WorkspaceMembership.WorkspaceRole


def _names(response):
    return {row["workspace"].name for row in response.context["rows"]}


@pytest.mark.django_db
def test_lists_the_brands_the_user_pays_for_and_nothing_else(client):
    user = User.objects.create_user(email="owner@a.com", password="x", tos_accepted_at=timezone.now())
    own_org = Organization.objects.create(name="Mine")
    # The organization in use: the page is about one organization at a time.
    OrgMembership.objects.filter(user=user).delete()
    OrgMembership.objects.create(user=user, organization=own_org, org_role=OrgMembership.OrgRole.OWNER)
    Workspace.objects.create(organization=own_org, name="Own brand")  # org owner, not a brand member

    other_org = Organization.objects.create(name="Theirs")
    managed = Workspace.objects.create(organization=other_org, name="Managed brand")
    WorkspaceMembership.objects.create(user=user, workspace=managed, workspace_role=ROLE.MANAGER)
    edited = Workspace.objects.create(organization=other_org, name="Edited brand")
    WorkspaceMembership.objects.create(user=user, workspace=edited, workspace_role=ROLE.EDITOR)
    Workspace.objects.create(organization=other_org, name="Unrelated brand")
    client.force_login(user)

    response = client.get(reverse("billing_account:overview"))

    assert response.status_code == 200
    names = _names(response)
    assert {"Own brand", "Managed brand"} <= names
    assert "Edited brand" not in names  # editors can't spend the organization's money
    assert "Unrelated brand" not in names


@pytest.mark.django_db
def test_listing_creates_no_subscription_rows(client):
    from apps.billing.models import WorkspaceSubscription

    user = User.objects.create_user(email="owner@a.com", password="x", tos_accepted_at=timezone.now())
    before = WorkspaceSubscription.objects.count()
    client.force_login(user)

    client.get(reverse("billing_account:overview"))

    assert WorkspaceSubscription.objects.count() == before


def _owner_with_brand(email="payer@a.com"):
    """A signed-up user: own organization, one brand, owner of both."""
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    workspace = WorkspaceMembership.objects.get(user=user).workspace
    return user, workspace


def _payment_event(event_id, event_type, subscription, payment_id="pay_1", value=49.9):
    from apps.billing.models import BillingEvent

    return BillingEvent.objects.create(
        gateway_event_id=event_id,
        event_type=event_type,
        status=BillingEvent.Status.PROCESSED,
        subscription=subscription,
        payload={"payment": {"id": payment_id, "value": value, "invoiceUrl": "https://asaas.test/i/1"}},
    )


@pytest.mark.django_db
def test_brands_are_counted_by_what_the_customer_can_do_with_them(client):
    from apps.billing import services

    user, trial = _owner_with_brand()
    org = trial.organization
    paid = Workspace.objects.create(organization=org, name="Paid")
    Workspace.objects.create(organization=org, name="Stopped")
    sub = services.ensure_subscription(trial)
    sub.trial_ends_at = timezone.now() + datetime.timedelta(days=3)
    sub.save(update_fields=["trial_ends_at"])
    sub = services.ensure_subscription(paid)
    sub.status = "active"
    sub.price_cents = 4990
    sub.save(update_fields=["status", "price_cents"])
    client.force_login(user)

    context = client.get(reverse("billing_account:overview")).context

    assert context["counts"] == {"all": 3, "active": 1, "inactive": 1, "trial": 1}
    assert context["plan"]["brands"] == 1
    assert context["plan"]["recurring_cents"] == 4990


@pytest.mark.django_db
def test_a_charge_is_listed_once_in_its_latest_state(client):
    from apps.billing import services

    user, workspace = _owner_with_brand()
    sub = services.ensure_subscription(workspace)
    _payment_event("evt_1", "PAYMENT_OVERDUE", sub)
    _payment_event("evt_2", "PAYMENT_CONFIRMED", sub)
    client.force_login(user)

    payments = client.get(reverse("billing_account:overview")).context["payments"]

    assert [(p["outcome"], p["amount_cents"]) for p in payments] == [("paid", 4990)]
    assert payments[0]["invoice_url"] == "https://asaas.test/i/1"


@pytest.mark.django_db
def test_payments_of_other_accounts_are_not_listed(client):
    from apps.billing import services

    user, _workspace = _owner_with_brand()
    _other, foreign = _owner_with_brand("other@a.com")
    _payment_event("evt_9", "PAYMENT_CONFIRMED", services.ensure_subscription(foreign))
    client.force_login(user)

    assert client.get(reverse("billing_account:overview")).context["payments"] == []


@pytest.mark.django_db
def test_the_payer_document_is_never_shown_whole(client):
    from apps.billing import services

    user, workspace = _owner_with_brand()
    profile = services.ensure_billing_profile(workspace.organization)
    profile.tax_id = "52998224725"
    profile.save(update_fields=["tax_id"])
    client.force_login(user)

    response = client.get(reverse("billing_account:overview"))

    assert response.context["payer"]["tax_id_ending"] == "4725"
    assert "52998224725" not in response.content.decode()


class _PayerGateway:
    """Stands in for Asaas when the payer's data is changed."""

    def __init__(self, error=None):
        self.error = error
        self.updates = []

    def update_customer(self, customer_id, **fields):
        if self.error:
            raise self.error
        self.updates.append((customer_id, fields))
        return {"id": customer_id}


def _post_payer(client, **overrides):
    data = {
        "payer_name": "Aurora Comércio Ltda",
        "tax_id": "11.222.333/0001-81",
        "phone": "(11) 98888-7777",
        "billing_email": "financeiro@aurora.com",
    }
    data.update(overrides)
    return client.post(reverse("billing_account:update_payer"), data)


@pytest.mark.django_db
def test_the_owner_changes_who_the_invoices_are_issued_to(client):
    from apps.billing import services

    user, workspace = _owner_with_brand()
    client.force_login(user)

    _post_payer(client)

    profile = services.ensure_billing_profile(workspace.organization)
    workspace.organization.refresh_from_db()
    assert (profile.payer_name, profile.tax_id, profile.phone) == (
        "Aurora Comércio Ltda",
        "11222333000181",
        "11988887777",
    )
    assert workspace.organization.billing_email == "financeiro@aurora.com"


@pytest.mark.django_db
def test_an_existing_asaas_customer_receives_the_change(client):
    from unittest import mock

    from apps.billing import services

    user, workspace = _owner_with_brand()
    profile = services.ensure_billing_profile(workspace.organization)
    profile.gateway, profile.gateway_customer_id, profile.tax_id = "asaas", "cus_1", "52998224725"
    profile.save()
    gateway = _PayerGateway()
    client.force_login(user)

    with mock.patch("apps.billing.contracting.AsaasClient", return_value=gateway):
        _post_payer(client, tax_id="")

    customer_id, fields = gateway.updates[0]
    assert customer_id == "cus_1"
    assert fields["name"] == "Aurora Comércio Ltda"
    assert fields["cpf_cnpj"] == "52998224725"  # left empty: the document on file is kept


@pytest.mark.django_db
def test_a_refusal_from_asaas_changes_nothing_here(client):
    from unittest import mock

    from apps.billing import services
    from apps.billing.gateways.asaas import AsaasAPIError

    user, workspace = _owner_with_brand()
    profile = services.ensure_billing_profile(workspace.organization)
    profile.gateway, profile.gateway_customer_id, profile.tax_id = "asaas", "cus_1", "52998224725"
    profile.save()
    client.force_login(user)

    with mock.patch(
        "apps.billing.contracting.AsaasClient",
        return_value=_PayerGateway(error=AsaasAPIError("bad", status_code=400)),
    ):
        _post_payer(client)

    profile.refresh_from_db()
    assert (profile.payer_name, profile.tax_id) == ("", "52998224725")


@pytest.mark.django_db
def test_an_invalid_document_is_not_saved(client):
    from apps.billing import services

    user, workspace = _owner_with_brand()
    client.force_login(user)

    _post_payer(client, tax_id="111.111.111-11")

    assert services.ensure_billing_profile(workspace.organization).payer_name == ""


@pytest.mark.django_db
def test_a_plain_member_cannot_change_the_payer(client):
    _owner, workspace = _owner_with_brand()
    member = User.objects.create_user(email="member@a.com", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=member).update(org_role=OrgMembership.OrgRole.MEMBER)
    client.force_login(member)

    assert _post_payer(client).status_code == 403


@pytest.mark.django_db
def test_changing_the_document_does_not_free_the_trial_it_spent(client, settings):
    from apps.billing import services

    settings.BILLING_TRIAL_DAYS = 7
    user, workspace = _owner_with_brand()
    services.start_trial(workspace, tax_id="52998224725", phone="11988887777")
    client.force_login(user)
    _post_payer(client)  # the account moves to another document

    _other, other_workspace = _owner_with_brand("again@b.com")
    with pytest.raises(services.TrialRefusedError):
        services.start_trial(other_workspace, tax_id="52998224725", phone="11977776666")
