"""Account centre → Billing: the brands whose billing the user manages, only."""

import datetime

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

ROLE = WorkspaceMembership.WorkspaceRole


def _names(response):
    return {row["workspace"].name for group in response.context["groups"] for row in group["rows"]}


@pytest.mark.django_db
def test_lists_the_brands_the_user_pays_for_and_nothing_else(client):
    user = User.objects.create_user(email="owner@a.com", password="x", tos_accepted_at=timezone.now())
    own_org = Organization.objects.create(name="Mine")
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
    # Only what the user owns or administers: a role in someone else's brand shows nothing of theirs.
    assert _names(response) == {"Own brand"}


@pytest.mark.django_db
def test_every_organization_the_user_runs_has_its_block(client):
    user = User.objects.create_user(email="owner@a.com", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).delete()
    owned = Organization.objects.create(name="Owned")
    administered = Organization.objects.create(name="Administered")
    only_member = Organization.objects.create(name="Only a member")
    OrgMembership.objects.create(user=user, organization=owned, org_role=OrgMembership.OrgRole.OWNER)
    OrgMembership.objects.create(user=user, organization=administered, org_role=OrgMembership.OrgRole.ADMIN)
    OrgMembership.objects.create(user=user, organization=only_member, org_role=OrgMembership.OrgRole.MEMBER)
    Workspace.objects.create(organization=owned, name="Brand A")
    Workspace.objects.create(organization=administered, name="Brand B")
    Workspace.objects.create(organization=only_member, name="Brand C")
    client.force_login(user)

    groups = client.get(reverse("billing_account:overview")).context["groups"]

    assert {g["organization"].name: [r["workspace"].name for r in g["rows"]] for g in groups} == {
        "Owned": ["Brand A"],
        "Administered": ["Brand B"],
    }


@pytest.mark.django_db
def test_someone_invited_to_a_brand_has_no_billing_page_and_cannot_contract(client):
    # Billing goes out in the organization's name: its owner's and admins', whatever the role in the brand.
    organization = Organization.objects.create(name="Theirs")
    brand = Workspace.objects.create(organization=organization, name="Managed brand")
    guest = User.objects.create_user(email="guest@a.com", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=guest).delete()
    OrgMembership.objects.create(user=guest, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(user=guest, workspace=brand, workspace_role=ROLE.MANAGER)
    client.force_login(guest)

    assert client.get(reverse("billing_account:overview")).status_code == 403
    assert client.get(reverse("billing:checkout", kwargs={"workspace_id": brand.id})).status_code == 403
    # The account menu does not offer what would answer 403.
    organizations = client.get(reverse("organizations:workspaces")).content.decode()
    assert reverse("billing_account:overview") not in organizations


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
        organization=subscription.workspace.organization,
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

    (group,) = client.get(reverse("billing_account:overview")).context["groups"]

    assert group["organization"] == org
    assert group["counts"] == {"all": 3, "active": 1, "inactive": 1, "trial": 1}
    assert group["plan"]["brands"] == 1
    assert group["plan"]["recurring_cents"] == 4990


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
def test_the_payer_document_is_shown_to_who_runs_the_organization_only(client):
    from apps.billing import services

    user, workspace = _owner_with_brand()
    organization = workspace.organization
    profile = services.ensure_billing_profile(organization)
    profile.tax_id = "52998224725"
    profile.save(update_fields=["tax_id"])
    client.force_login(user)

    # The owner sees and edits it whole, in the organization's modal.
    assert "529.982.247-25" in client.get(reverse("organizations:workspaces")).content.decode()

    # A member of the organization, even managing its brand, sees no digit of it.
    OrgMembership.objects.filter(user=user, organization=organization).update(org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.filter(user=user, workspace=workspace).update(workspace_role=ROLE.MANAGER)
    assert client.get(reverse("billing_account:overview")).status_code == 403
    page = client.get(reverse("organizations:workspaces")).content.decode()
    assert "529.982.247-25" not in page and "52998224725" not in page and "247-25" not in page


@pytest.mark.django_db
def test_the_organization_is_edited_with_its_billing_data(client, monkeypatch):
    from apps.billing import services

    user, workspace = _owner_with_brand()
    organization = workspace.organization
    client.force_login(user)
    form = {
        "organization": str(organization.id),
        "name": "Aurora",
        "payer_name": "Aurora Comércio Ltda",
        "tax_id": "11.222.333/0001-81",
        "phone": "(11) 98888-7777",
        "billing_email": "financeiro@aurora.com",
    }

    client.post(reverse("organizations:edit"), form)

    organization.refresh_from_db()
    profile = services.ensure_billing_profile(organization)
    assert (organization.name, organization.billing_email) == ("Aurora", "financeiro@aurora.com")
    assert (profile.payer_name, profile.tax_id, profile.phone) == (
        "Aurora Comércio Ltda",
        "11222333000181",
        "11988887777",
    )

    # Refused billing data leaves the name as it was too.
    client.post(reverse("organizations:edit"), {**form, "name": "Other name", "tax_id": "123"})
    organization.refresh_from_db()
    assert organization.name == "Aurora"

    # Someone who does not run the organization cannot save it.
    OrgMembership.objects.filter(user=user, organization=organization).update(org_role=OrgMembership.OrgRole.MEMBER)
    assert client.post(reverse("organizations:edit"), {**form, "name": "Hijacked"}).status_code == 403
    organization.refresh_from_db()
    assert organization.name == "Aurora"


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


@pytest.mark.django_db
def test_the_history_keeps_the_charges_of_an_archived_or_deleted_brand(client):
    from apps.billing import services

    user, workspace = _owner_with_brand()
    organization = workspace.organization
    archived = Workspace.objects.create(organization=organization, name="Archived brand", is_archived=True)
    _payment_event("evt_1", "PAYMENT_CONFIRMED", services.ensure_subscription(archived), payment_id="pay_a")
    gone = Workspace.objects.create(organization=organization, name="Gone brand")
    _payment_event("evt_2", "PAYMENT_CONFIRMED", services.ensure_subscription(gone), payment_id="pay_b")
    gone.delete()
    client.force_login(user)

    response = client.get(reverse("billing_account:overview"))

    assert {p["workspace"].name if p["workspace"] else None for p in response.context["payments"]} == {
        "Archived brand",
        None,
    }
    # The archived brand is not among the brands to contract, only in the history.
    assert "Archived brand" not in _names(response)


@pytest.mark.django_db
@override_settings(ASAAS_WEBHOOK_TOKEN="tok", RATELIMIT_ENABLE=False)
def test_a_payment_of_the_plan_is_in_the_history(client):
    from apps.billing.models import BillingEvent, OrganizationPlan

    user, workspace = _owner_with_brand()
    organization = workspace.organization
    OrganizationPlan.objects.create(organization=organization, brands=1, gateway_subscription_id="sub_plan")

    client.post(
        reverse("asaas_webhook"),
        data={
            "id": "evt_plan",
            "event": "PAYMENT_CONFIRMED",
            "payment": {"id": "pay_p", "subscription": "sub_plan", "value": 50},
        },
        content_type="application/json",
        headers={"asaas-access-token": "tok"},
    )

    assert BillingEvent.objects.get(gateway_event_id="evt_plan").organization == organization
    client.force_login(user)
    payments = client.get(reverse("billing_account:overview")).context["payments"]
    assert [(p["outcome"], p["amount_cents"], p["workspace"]) for p in payments] == [("paid", 5000, None)]
    # Said for what it is, with the plan it belongs to.
    assert (payments[0]["kind"], payments[0]["plan"].organization) == ("plan", organization)


@pytest.mark.django_db
def test_events_logged_before_are_given_their_organization():
    import importlib

    from django.apps import apps as global_apps

    from apps.billing import services
    from apps.billing.models import BillingEvent, OrganizationPlan

    backfill = importlib.import_module("apps.billing.migrations.0013_backfill_billingevent_organization")
    _user, workspace = _owner_with_brand()
    organization = workspace.organization
    OrganizationPlan.objects.create(organization=organization, brands=1, gateway_subscription_id="sub_plan")
    of_a_brand = _payment_event("evt_1", "PAYMENT_CONFIRMED", services.ensure_subscription(workspace))
    of_the_plan = BillingEvent.objects.create(
        gateway_event_id="evt_2",
        event_type="PAYMENT_CONFIRMED",
        status=BillingEvent.Status.PROCESSED,
        payload={"payment": {"id": "pay_2", "subscription": "sub_plan"}},
    )
    of_nobody = BillingEvent.objects.create(gateway_event_id="evt_3", payload={"payment": "odd"})
    BillingEvent.objects.update(organization=None)

    backfill.fill_organization(global_apps, None)
    backfill.fill_organization(global_apps, None)

    of_a_brand.refresh_from_db(), of_the_plan.refresh_from_db(), of_nobody.refresh_from_db()
    assert of_a_brand.organization == organization
    assert of_the_plan.organization == organization
    assert of_nobody.organization is None
