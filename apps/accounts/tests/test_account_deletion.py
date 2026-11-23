"""Deleting a customer's account from the administrative area: what goes with it,
what stays, the gateway subscriptions, and who may do it."""

from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.deletion import delete_account
from apps.accounts.models import User
from apps.billing import services
from apps.billing.gateways.asaas import AsaasAPIError
from apps.members.models import OrgMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

CANCEL = "apps.billing.gateways.asaas.AsaasClient.cancel_subscription"


def _user(email, *, staff=False):
    return User.objects.create_user(
        email=email, password="x", tos_accepted_at=timezone.now(), is_staff=staff, is_superuser=staff
    )


def _org(name, owner, *, gateway_id=""):
    org = Organization.objects.create(name=name)
    OrgMembership.objects.create(user=owner, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    workspace = Workspace.objects.create(organization=org, name=f"{name} brand")
    if gateway_id:
        sub = services.ensure_subscription(workspace)
        sub.gateway_subscription_id = gateway_id
        sub.save(update_fields=["gateway_subscription_id"])
    return org


def _clear_signup_orgs(*users):
    """Users get an organization of their own at creation; these tests build theirs by hand."""
    Organization.objects.filter(memberships__user__in=users).delete()


def test_the_account_goes_with_the_organizations_only_it_owns():
    owner, partner, member = _user("owner@example.com"), _user("partner@example.com"), _user("member@example.com")
    _clear_signup_orgs(owner, partner, member)
    solo = _org("Solo", owner)
    OrgMembership.objects.create(user=member, organization=solo, org_role=OrgMembership.OrgRole.MEMBER)
    shared = _org("Shared", owner)
    OrgMembership.objects.create(user=partner, organization=shared, org_role=OrgMembership.OrgRole.OWNER)

    assert delete_account(owner) == ["Solo"]

    assert not User.objects.filter(pk=owner.pk).exists()
    assert not Organization.objects.filter(pk=solo.pk).exists()
    assert not Workspace.objects.filter(organization_id=solo.pk).exists()
    assert Organization.objects.filter(pk=shared.pk).exists()
    # The other member of the deleted organization lands on a new one of their own.
    assert OrgMembership.objects.filter(user=member).exists()
    assert not Organization.objects.filter(memberships__user_id=owner.pk).exists()


def test_the_gateway_subscriptions_are_cancelled_before_the_organization_goes():
    owner = _user("owner@example.com")
    _clear_signup_orgs(owner)
    _org("Paying", owner, gateway_id="sub_123")

    with mock.patch(CANCEL) as cancel, mock.patch("apps.billing.gateways.asaas.AsaasClient.is_configured", True):
        delete_account(owner)

    cancel.assert_called_once_with("sub_123")


def test_a_gateway_refusal_keeps_the_account_and_its_organization():
    owner = _user("owner@example.com")
    _clear_signup_orgs(owner)
    org = _org("Paying", owner, gateway_id="sub_123")

    with (
        mock.patch(CANCEL, side_effect=AsaasAPIError("down", status_code=503)),
        mock.patch("apps.billing.gateways.asaas.AsaasClient.is_configured", True),
        pytest.raises(AsaasAPIError),
    ):
        delete_account(owner)

    assert User.objects.filter(pk=owner.pk).exists()
    assert Organization.objects.filter(pk=org.pk).exists()
    assert OrgMembership.objects.filter(user=owner, organization=org).exists()


def test_only_staff_delete_and_never_an_operator_account(client):
    customer, colleague = _user("customer@example.com"), _user("colleague@example.com", staff=True)
    url = reverse("billing_staff:delete_user", kwargs={"target_user_id": customer.id})

    client.force_login(_user("member@example.com"))
    assert client.post(url, {"confirm_email": customer.email}).status_code == 403

    operator = _user("op@example.com", staff=True)
    client.force_login(operator)
    colleague_url = reverse("billing_staff:delete_user", kwargs={"target_user_id": colleague.id})
    assert client.post(colleague_url, {"confirm_email": colleague.email}).status_code == 403
    own_url = reverse("billing_staff:delete_user", kwargs={"target_user_id": operator.id})
    assert client.post(own_url, {"confirm_email": operator.email}).status_code == 403
    assert User.objects.filter(pk__in=[customer.pk, colleague.pk, operator.pk]).count() == 3


def test_the_email_must_be_typed_back(client):
    customer = _user("customer@example.com")
    client.force_login(_user("op@example.com", staff=True))
    url = reverse("billing_staff:delete_user", kwargs={"target_user_id": customer.id})

    client.post(url, {"confirm_email": "someone@example.com"})
    assert User.objects.filter(pk=customer.pk).exists()

    response = client.post(url, {"confirm_email": " Customer@Example.com "})
    assert response.status_code == 302
    assert not User.objects.filter(pk=customer.pk).exists()
