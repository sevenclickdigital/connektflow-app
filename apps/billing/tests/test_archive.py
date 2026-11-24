"""After a plan ends the organization is archived: kept for a while, resumable, then removed with notice."""

from __future__ import annotations

import datetime

import pytest
from dateutil.relativedelta import relativedelta
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.billing import contracting, services
from apps.billing.gateways.asaas import AsaasAPIError
from apps.billing.models import OrganizationPlan, WorkspaceSubscription
from apps.billing.tests.test_plan_checkout import StubAsaas, _buy, _org_with_workspaces, _prices  # noqa: F401
from apps.members.models import OrgMembership
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db

ACTIVE = WorkspaceSubscription.Status.ACTIVE
ENDS_ON = datetime.date(2027, 1, 10)
ENDED_AT = timezone.make_aware(datetime.datetime(2027, 1, 10, 12))
REMOVAL_AT = ENDED_AT + relativedelta(months=6)
DAY = datetime.timedelta(days=1)


class Gateway(StubAsaas):
    """Asaas with the plan's next charge on ``ENDS_ON``, keeping the day each subscription starts charging."""

    def __init__(self, **kwargs):
        super().__init__(payments=[{"id": "pay_2", "status": "PENDING", "dueDate": ENDS_ON.isoformat()}], **kwargs)
        self.due_dates = []

    def create_subscription(self, *, next_due_date=None, **kwargs):
        self.due_dates.append(next_due_date)
        return super().create_subscription(**kwargs)


def _cancelled_whole():
    org, workspaces = _org_with_workspaces(2)
    plan = _buy(org, brands=2, client=StubAsaas())
    contracting.cancel_plan_brands(plan, workspaces, client=Gateway())
    plan.refresh_from_db()
    return org, plan, workspaces


def _archived(ended_at=ENDED_AT):
    org, plan, workspaces = _cancelled_whole()
    services.end_cancelled_subscriptions(today=ENDS_ON)
    OrganizationPlan.objects.filter(pk=plan.pk).update(canceled_at=ended_at)
    plan.refresh_from_db()
    return org, plan, workspaces


@pytest.fixture
def gateway(monkeypatch):
    """What removing an organization reaches Asaas through."""
    stub = StubAsaas()
    monkeypatch.setattr(contracting, "AsaasClient", lambda: stub)
    return stub


# -- undoing the cancellation while the paid period runs -------------------------


def test_undoing_the_cancellation_renews_on_the_day_it_was_to_end_with_the_same_card():
    _org, plan, workspaces = _cancelled_whole()
    gateway = Gateway()

    contracting.resume_plan(plan, remote_ip="177.0.0.1", client=gateway)

    plan.refresh_from_db()
    assert gateway.subscriptions == [{"value_cents": 10000, "cycle": "MONTHLY"}]
    assert gateway.due_dates[0] >= ENDS_ON
    assert gateway.cards_kept == [("sub_1", "tok_1")] and gateway.charged == []
    assert (plan.status, plan.cancels_on) == (ACTIVE, None)
    assert not WorkspaceSubscription.objects.filter(plan=plan, cancels_on__isnull=False).exists()
    assert services.end_cancelled_subscriptions(today=ENDS_ON) == 0
    assert all(services.is_workspace_active(workspace.id) for workspace in workspaces)


def test_the_plan_stays_cancelled_when_the_card_cannot_be_put_on_the_new_subscription():
    _org, plan, _workspaces = _cancelled_whole()

    class Refusing(Gateway):
        def set_subscription_card(self, subscription_id, *, card_token, remote_ip):
            raise AsaasAPIError("Asaas returned HTTP 400.", status_code=400, errors=[])

    gateway = Refusing()
    with pytest.raises(AsaasAPIError):
        contracting.resume_plan(plan, client=gateway)

    plan.refresh_from_db()
    # The subscription just created is withdrawn: no invoice reaches the customer.
    assert gateway.canceled == ["sub_1"]
    assert plan.cancels_on == ENDS_ON


def test_only_a_plan_cancelled_whole_with_its_card_kept_can_be_undone():
    org, workspaces = _org_with_workspaces(1)
    plan = _buy(org, client=StubAsaas())
    with pytest.raises(contracting.NothingToCancelError):
        contracting.resume_plan(plan, client=Gateway())

    contracting.cancel_plan_brands(plan, workspaces, client=Gateway())
    OrganizationPlan.objects.filter(pk=plan.pk).update(gateway_card_token=None)
    plan.refresh_from_db()
    gateway = Gateway()
    with pytest.raises(contracting.NothingToCancelError):
        contracting.resume_plan(plan, client=gateway)
    assert gateway.subscriptions == []


def test_only_who_runs_the_organization_undoes_its_cancellation(client, owner_of, monkeypatch):
    org, _plan, _workspaces = _cancelled_whole()
    monkeypatch.setattr(contracting, "AsaasClient", lambda: pytest.fail("Asaas must not be called"))
    url = reverse("billing_account:resume", kwargs={"organization_id": org.id})

    client.force_login(owner_of(org, role=OrgMembership.OrgRole.MEMBER))
    assert client.post(url).status_code == 404

    other, _ = _org_with_workspaces(1, name="Other")
    client.force_login(owner_of(other, role=OrgMembership.OrgRole.ADMIN))
    assert client.post(url).status_code == 404
    assert OrganizationPlan.objects.get(organization=org).cancels_on == ENDS_ON


# -- archived --------------------------------------------------------------------


def test_an_organization_is_archived_once_its_plan_ends_and_until_it_is_contracted_again():
    org, plan, _workspaces = _cancelled_whole()
    assert services.archived_plan(org) is None

    services.end_cancelled_subscriptions(today=ENDS_ON)
    OrganizationPlan.objects.filter(pk=plan.pk).update(canceled_at=ENDED_AT)

    archived = services.archived_plan(org)
    assert archived == plan
    assert services.archive_removal_at(archived, now=ENDED_AT) == REMOVAL_AT

    _buy(org, brands=2, client=StubAsaas())
    assert services.archived_plan(org) is None


def test_the_billing_page_offers_to_resume_an_archived_organization(client, owner_of):
    org, _plan, workspaces = _archived(ended_at=timezone.now())
    client.force_login(owner_of(org))

    response = client.get(reverse("billing_account:overview"))

    (group,) = response.context["groups"]
    assert group["archived_until"] is not None
    checkout = reverse("billing:checkout", kwargs={"workspace_id": group["resume_brand"].id})
    assert group["resume_brand"] in workspaces and checkout in response.content.decode()
    assert client.get(checkout).status_code == 200


def test_an_archive_is_warned_a_month_before_and_removed_when_the_six_months_end(owner_of, gateway):
    org, plan, _workspaces = _archived()
    owner = owner_of(org)
    warning_day = REMOVAL_AT - services.ARCHIVE_WARNING

    assert services.remove_expired_archives(now=warning_day - DAY) == (0, 0)
    assert mail.outbox == []

    assert services.remove_expired_archives(now=warning_day) == (1, 0)
    (message,) = mail.outbox
    assert message.to == [owner.email]
    assert reverse("billing_account:overview") in message.body
    # Asked again, the same archive is neither warned twice nor removed early.
    assert services.remove_expired_archives(now=REMOVAL_AT - DAY) == (0, 0)
    assert len(mail.outbox) == 1 and Organization.objects.filter(pk=org.pk).exists()

    assert services.remove_expired_archives(now=REMOVAL_AT) == (0, 1)
    assert not Organization.objects.filter(pk=org.pk).exists()
    assert gateway.canceled == [plan.gateway_subscription_id]


def test_an_archive_older_than_six_months_still_gets_its_full_notice(owner_of, gateway):
    org, _plan, _workspaces = _archived(ended_at=ENDED_AT - relativedelta(years=2))
    owner_of(org)

    assert services.remove_expired_archives(now=ENDED_AT) == (1, 0)
    assert services.remove_expired_archives(now=ENDED_AT + services.ARCHIVE_WARNING - DAY) == (0, 0)
    assert Organization.objects.filter(pk=org.pk).exists()
    assert services.remove_expired_archives(now=ENDED_AT + services.ARCHIVE_WARNING) == (0, 1)


def test_nothing_is_removed_while_the_warning_could_not_be_sent(gateway):
    # Nobody to write to: no owner and no billing address.
    org, _plan, _workspaces = _archived()

    assert services.remove_expired_archives(now=REMOVAL_AT + relativedelta(years=1)) == (0, 0)
    assert Organization.objects.filter(pk=org.pk).exists()


def test_an_organization_resumed_after_its_warning_is_kept_and_warned_anew_if_archived_again(owner_of, gateway):
    org, plan, _workspaces = _archived()
    owner_of(org)
    services.remove_expired_archives(now=REMOVAL_AT - services.ARCHIVE_WARNING)

    _buy(org, brands=2, client=StubAsaas())
    assert services.remove_expired_archives(now=REMOVAL_AT + DAY) == (0, 0)
    assert Organization.objects.filter(pk=org.pk).exists()

    # Archived a second time: the warning of the first time does not count.
    again = REMOVAL_AT + relativedelta(years=1)
    OrganizationPlan.objects.filter(pk=plan.pk).update(status=WorkspaceSubscription.Status.CANCELED, canceled_at=again)
    WorkspaceSubscription.objects.filter(plan=plan).update(status=WorkspaceSubscription.Status.CANCELED)
    assert services.remove_expired_archives(now=again + relativedelta(months=6)) == (1, 0)
    assert Organization.objects.filter(pk=org.pk).exists()


def test_an_organization_with_a_brand_still_in_use_is_not_an_archive(owner_of, gateway):
    org, _plan, (first, _second) = _archived()
    owner_of(org)
    services.grant_courtesy(first)

    assert services.archived_plan(org) is None
    assert services.remove_expired_archives(now=REMOVAL_AT + relativedelta(years=1)) == (0, 0)
    assert Organization.objects.filter(pk=org.pk).exists()
