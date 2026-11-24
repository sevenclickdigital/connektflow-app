"""The e-mail that congratulates the payer when a payment puts the plan in use."""

from __future__ import annotations

import pytest
from django.core import mail
from django.test import override_settings
from django.urls import reverse

from apps.billing.models import WorkspaceSubscription
from apps.billing.tests.test_plan_checkout import StubAsaas, _buy, _org_with_workspaces, _prices  # noqa: F401

pytestmark = pytest.mark.django_db


def _event(client, event, event_id, subscription="sub_1"):
    payload = {"id": event_id, "event": event, "payment": {"id": f"pay_{event_id}", "subscription": subscription}}
    return client.post(
        reverse("asaas_webhook"),
        data=payload,
        content_type="application/json",
        headers={"asaas-access-token": "tok"},
    )


def test_paying_by_card_congratulates_the_payer_with_the_plan(owner_of, django_capture_on_commit_callbacks):
    org, _workspaces = _org_with_workspaces(2)
    owner_of(org)
    org.billing_email = "ana@example.com"
    org.save(update_fields=["billing_email"])

    with django_capture_on_commit_callbacks(execute=True):
        _buy(org, brands=2, client=StubAsaas())

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    # The billing address of the organization, when it has one, rather than the owner's.
    assert message.to == ["ana@example.com"]
    assert "Acme" in message.body
    assert "2 marcas" in message.body
    assert "100,00" in message.body
    html = message.alternatives[0][0]
    assert "Acme" in html and reverse("billing_account:overview") in html


@override_settings(ASAAS_WEBHOOK_TOKEN="tok", RATELIMIT_ENABLE=False)
def test_the_gateway_confirming_what_is_already_active_sends_nothing(
    client, owner_of, django_capture_on_commit_callbacks
):
    org, _workspaces = _org_with_workspaces(1)
    owner_of(org)
    with django_capture_on_commit_callbacks(execute=True):
        _buy(org, client=StubAsaas())
    mail.outbox.clear()

    # The confirmation of the charge just paid, then next month's renewal.
    with django_capture_on_commit_callbacks(execute=True):
        _event(client, "PAYMENT_CONFIRMED", "evt_1")
        _event(client, "PAYMENT_RECEIVED", "evt_2")

    assert mail.outbox == []


@override_settings(ASAAS_WEBHOOK_TOKEN="tok", RATELIMIT_ENABLE=False)
def test_a_payment_that_recovers_an_overdue_plan_is_confirmed(client, owner_of, django_capture_on_commit_callbacks):
    org, _workspaces = _org_with_workspaces(1)
    owner = owner_of(org)
    with django_capture_on_commit_callbacks(execute=True):
        _buy(org, client=StubAsaas())
        _event(client, "PAYMENT_OVERDUE", "evt_1")
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        _event(client, "PAYMENT_CONFIRMED", "evt_2")

    assert [m.to for m in mail.outbox] == [[owner.email]]


def test_without_a_billing_address_it_goes_to_the_owner(owner_of, django_capture_on_commit_callbacks):
    from apps.billing import emails

    org, (workspace,) = _org_with_workspaces(1)
    owner = owner_of(org)

    with django_capture_on_commit_callbacks(execute=True):
        emails.send_payment_confirmed(org, workspace=workspace)

    assert [m.to for m in mail.outbox] == [[owner.email]]
    assert workspace.name in mail.outbox[0].body


def test_a_failed_send_does_not_undo_the_payment(owner_of, django_capture_on_commit_callbacks, monkeypatch):
    from apps.billing import emails

    def boom(_message):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(emails, "send_or_raise", boom)
    org, (workspace,) = _org_with_workspaces(1)
    owner_of(org)

    with django_capture_on_commit_callbacks(execute=True):
        plan = _buy(org, client=StubAsaas())

    assert plan.status == WorkspaceSubscription.Status.ACTIVE
    assert mail.outbox == []


def test_a_card_charge_at_checkout_is_in_the_payment_history_at_once(client, owner_of):
    # The history is read from the gateway's events; the checkout logs its own
    # charge, so it does not wait for a webhook that may never reach this machine.
    from apps.billing.models import BillingEvent

    org, _workspaces = _org_with_workspaces(1)
    owner = owner_of(org)

    plan = _buy(org, client=StubAsaas())

    logged = BillingEvent.objects.get(organization=org)
    assert logged.gateway_event_id.startswith("checkout:")
    client.force_login(owner)
    context = client.get(reverse("billing_account:overview")).context
    assert [p["outcome"] for p in context["payments"]] == ["paid"]
    # The brand is on the plan: the organization pays what the plan charges, not the brand's list price.
    (group,) = [g for g in context["groups"] if g["organization"] == org]
    assert group["plan"]["recurring_cents"] == plan.price_cents
    assert [row["on_plan"] for row in group["rows"]] == [True]
