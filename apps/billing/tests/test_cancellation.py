"""Cancelling brands of a plan: in use until the end of the paid period, never charged again."""

from __future__ import annotations

import datetime

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.billing import contracting, services
from apps.billing.models import OrganizationPlan, WorkspaceSubscription
from apps.billing.tests.test_plan_checkout import StubAsaas, _buy, _org_with_workspaces, _prices  # noqa: F401
from apps.members.models import OrgMembership

pytestmark = pytest.mark.django_db

ACTIVE = WorkspaceSubscription.Status.ACTIVE
CANCELED = WorkspaceSubscription.Status.CANCELED
ENDS_ON = datetime.date(2027, 1, 10)


def _gateway():
    """Asaas with the plan's next charge on ``ENDS_ON``."""
    return StubAsaas(payments=[{"id": "pay_2", "status": "PENDING", "dueDate": ENDS_ON.isoformat()}])


def _in_use(workspace):
    """Asked by id, so the answer is the row as stored and not the one cached on the instance."""
    return services.is_workspace_active(workspace.id)


def _row(workspace):
    return WorkspaceSubscription.objects.get(workspace=workspace)


def _plan_of_two():
    org, (first, second) = _org_with_workspaces(2)
    plan = _buy(org, brands=2, client=StubAsaas())
    return org, plan, first, second


def test_a_cancelled_brand_stays_in_use_until_the_next_charge_which_comes_without_it():
    _org, plan, first, second = _plan_of_two()
    gateway = _gateway()

    ends_on = contracting.cancel_plan_brands(plan, [second], client=gateway)

    assert ends_on == ENDS_ON
    # The gateway already charges for one brand; nothing was cancelled there.
    assert gateway.values == [("sub_1", 5000)] and gateway.canceled == []
    assert (_row(second).status, _row(second).cancels_on) == (ACTIVE, ENDS_ON)
    assert _in_use(second)
    assert _row(first).cancels_on is None

    # The day before, nothing ends; on the day, the brand leaves the plan.
    assert services.end_cancelled_subscriptions(today=ENDS_ON - datetime.timedelta(days=1)) == 0
    assert services.end_cancelled_subscriptions(today=ENDS_ON) == 1
    plan.refresh_from_db()
    assert (plan.status, plan.brands, plan.price_cents) == (ACTIVE, 1, 5000)
    assert (_row(second).status, _row(second).plan, _row(second).cancels_on) == (CANCELED, None, None)
    assert not _in_use(second)
    assert _in_use(first)
    # Run again: nothing left to close.
    assert services.end_cancelled_subscriptions(today=ENDS_ON) == 0


def test_cancelling_every_brand_stops_the_renewal_and_ends_the_plan_on_that_day():
    org, plan, first, second = _plan_of_two()
    gateway = _gateway()

    contracting.cancel_plan_brands(plan, [first, second], client=gateway)

    plan.refresh_from_db()
    assert gateway.canceled == ["sub_1"] and gateway.values == []
    assert (plan.status, plan.cancels_on) == (ACTIVE, ENDS_ON)
    assert _in_use(first) and _in_use(second)

    services.end_cancelled_subscriptions(today=ENDS_ON)

    plan.refresh_from_db()
    assert plan.status == CANCELED
    assert not _in_use(first) and not _in_use(second)

    # Contracted again later, the plan is not ended by the date of the old cancellation.
    org.refresh_from_db()
    again = _buy(org, brands=2, client=StubAsaas())
    assert (again.status, again.cancels_on) == (ACTIVE, None)
    assert services.end_cancelled_subscriptions(today=ENDS_ON + datetime.timedelta(days=400)) == 0
    assert _in_use(first)


@override_settings(ASAAS_WEBHOOK_TOKEN="tok", RATELIMIT_ENABLE=False)
def test_the_gateway_reporting_the_cancelled_subscription_deleted_does_not_lock_the_paid_period(client):
    _org, plan, first, second = _plan_of_two()
    contracting.cancel_plan_brands(plan, [first, second], client=_gateway())

    for number, event in enumerate(["PAYMENT_DELETED", "SUBSCRIPTION_DELETED"]):
        client.post(
            reverse("asaas_webhook"),
            data={"id": f"evt_{number}", "event": event, "payment": {"id": "pay_2", "subscription": "sub_1"}},
            content_type="application/json",
            headers={"asaas-access-token": "tok"},
        )

    plan.refresh_from_db()
    assert plan.status == ACTIVE
    assert _in_use(first)


def test_a_cancelled_brand_can_be_kept_and_the_next_charge_includes_it_again():
    _org, plan, _first, second = _plan_of_two()
    contracting.cancel_plan_brands(plan, [second], client=_gateway())
    gateway = _gateway()

    contracting.keep_plan_brand(plan, second, client=gateway)

    assert gateway.values == [("sub_1", 10000)]
    assert _row(second).cancels_on is None
    assert services.end_cancelled_subscriptions(today=ENDS_ON) == 0


def test_nothing_is_marked_when_the_gateway_refuses_or_nothing_was_ticked():
    from apps.billing.gateways.asaas import AsaasAPIError

    _org, plan, _first, second = _plan_of_two()

    class Refusing(StubAsaas):
        def update_subscription_value(self, subscription_id, value_cents):
            raise AsaasAPIError("Asaas returned HTTP 500.", status_code=500, errors=[])

    with pytest.raises(AsaasAPIError):
        contracting.cancel_plan_brands(plan, [second], client=Refusing(payments=_gateway().payments))
    with pytest.raises(contracting.NothingToCancelError):
        contracting.cancel_plan_brands(plan, [], client=_gateway())

    assert not WorkspaceSubscription.objects.filter(cancels_on__isnull=False).exists()


def test_only_who_runs_the_organization_reaches_the_cancel_page(client, owner_of, monkeypatch):
    org, plan, _first, second = _plan_of_two()
    owner = owner_of(org)
    monkeypatch.setattr("apps.billing.views.AsaasClient", _gateway)
    monkeypatch.setattr("apps.billing.contracting.AsaasClient", _gateway)
    url = reverse("billing_account:cancel", kwargs={"organization_id": org.id})

    client.force_login(owner)
    page = client.get(url)
    assert page.status_code == 200
    assert [row.workspace.name for row in page.context["rows"]] == ["Brand 1", "Brand 2"]
    assert page.context["ends_on"] == ENDS_ON
    # The billing page offers it, and shows the brand as cancelled afterwards.
    assert url in client.get(reverse("billing_account:overview")).content.decode()
    client.post(url, {"brands": [str(second.id)]})
    assert _row(second).cancels_on == ENDS_ON
    (group,) = [
        g for g in client.get(reverse("billing_account:overview")).context["groups"] if g["organization"] == org
    ]
    assert [row["cancels_on"] for row in group["rows"]] == [None, ENDS_ON]
    # One brand left: the next charge is the plan of one.
    assert group["plan"]["recurring_cents"] == 5000

    # A member of the organization, whatever their role in its brands, cannot.
    OrgMembership.objects.filter(user=owner, organization=org).update(org_role=OrgMembership.OrgRole.MEMBER)
    assert client.get(url).status_code == 404
    assert client.post(url, {"brands": [str(_first.id)]}).status_code == 404
    assert OrganizationPlan.objects.get(pk=plan.pk).cancels_on is None
    assert _row(_first).cancels_on is None
