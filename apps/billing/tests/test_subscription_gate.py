"""Tests for the lock on unpaid workspaces and the contracting flow behind it.

What a wording change must never break: who is let into a workspace, where
the others are sent, what the contracting form refuses, and that a new
account's first screen is the plan. Nothing here reaches Asaas — the
gateway client is replaced by the stub the contracting tests use.
"""

from __future__ import annotations

import datetime
from unittest import mock

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import services
from apps.billing.gateways.asaas import AsaasAPIError
from apps.billing.models import WorkspaceAddon, WorkspaceSubscription
from apps.billing.tests.test_contracting import StubAsaas
from apps.composer.models import Post
from apps.members.models import WorkspaceMembership
from apps.workspaces.models import Workspace

VALID_CPF = "529.982.247-25"


def _signed_up(email="new@example.com"):
    """A user as sign-up leaves them: own organization, one brand, owner."""
    user = User.objects.create_user(email=email, password="testpass123", tos_accepted_at=timezone.now())
    workspace = WorkspaceMembership.objects.get(user=user).workspace
    client = Client()
    client.force_login(user)
    return client, workspace


def _answer_about(client, workspace):
    """Answer the "about your business" step a new account goes through before its plan."""
    client.post(
        reverse("organizations:about"),
        {
            "workspace": str(workspace.id),
            "business_activity": "agency",
            "team_size": "solo",
            "referral_source": "google",
            "client_count": "1_5",
            "organization_name": "Agência Sol",
        },
    )


def _url(name, workspace):
    return reverse(name, kwargs={"workspace_id": workspace.id})


def _on_trial(workspace):
    return WorkspaceSubscription.objects.filter(workspace=workspace, trial_ends_at__isnull=False).exists()


@pytest.fixture
def required(settings):
    settings.BILLING_REQUIRE_SUBSCRIPTION = True


@pytest.fixture
def gateway(settings):
    """Asaas configured, answered by a stub."""
    settings.ASAAS_API_KEY = "test-key"
    stub = StubAsaas()
    with mock.patch("apps.billing.contracting.AsaasClient", return_value=stub):
        yield stub


@pytest.mark.django_db
class TestGate:
    def test_a_new_account_lands_on_the_plan_once_it_has_said_what_it_does(self, required):
        client, workspace = _signed_up()
        about = f"{reverse('organizations:about')}?workspace={workspace.id}"
        assert client.get(reverse("dashboard"), follow=True).redirect_chain[-1][0] == about

        _answer_about(client, workspace)

        response = client.get(reverse("dashboard"), follow=True)
        assert response.redirect_chain[-1][0] == _url("billing:checkout", workspace)
        assert response.status_code == 200

    def test_an_unpaid_workspace_is_not_opened(self, required):
        client, workspace = _signed_up()
        response = client.get(_url("calendar:calendar", workspace))
        assert response.status_code == 302
        assert response.headers["Location"] == _url("billing:subscribe_modal", workspace)

    def test_a_fragment_request_is_told_to_leave_the_page(self, required):
        client, workspace = _signed_up()
        response = client.get(_url("calendar:calendar", workspace), HTTP_HX_REQUEST="true")
        assert response.status_code == 204
        assert response.headers["HX-Redirect"] == _url("billing:subscribe_modal", workspace)

    @pytest.mark.parametrize("status", ["active", "courtesy"])
    def test_a_paid_or_comped_workspace_opens(self, required, status):
        client, workspace = _signed_up()
        sub = services.ensure_subscription(workspace)
        sub.status = status
        sub.save(update_fields=["status"])
        assert client.get(_url("calendar:calendar", workspace)).status_code == 200

    @pytest.mark.parametrize("status", ["past_due", "canceled"])
    def test_a_lapsed_workspace_is_locked_again(self, required, status):
        client, workspace = _signed_up()
        sub = services.ensure_subscription(workspace)
        sub.status = status
        sub.save(update_fields=["status"])
        assert client.get(_url("calendar:calendar", workspace)).status_code == 302

    def test_billing_pages_stay_open_to_a_locked_workspace(self, required):
        client, workspace = _signed_up()
        _answer_about(client, workspace)
        assert client.get(_url("billing:subscribe_modal", workspace), follow=True).status_code == 200
        assert client.get(_url("billing:checkout", workspace)).status_code == 200
        assert client.get(_url("billing:workspace_billing", workspace)).status_code == 200

    def test_nothing_is_locked_while_the_switch_is_off(self, settings):
        settings.BILLING_REQUIRE_SUBSCRIPTION = False
        client, workspace = _signed_up()
        assert client.get(_url("calendar:calendar", workspace)).status_code == 200

    def test_another_account_is_refused_not_sent_to_pay(self, required):
        _client, workspace = _signed_up()
        stranger, _own = _signed_up("stranger@example.com")
        assert stranger.get(_url("calendar:calendar", workspace)).status_code == 403


ACCOUNT_PAGES = [
    "members:list",
    "settings_manager:index",
    "notifications:list",
    "api_keys:list",
    "organizations:cross_workspace_calendar",
    "media_library_org:shared_index",
    "workspaces:list",
]


@pytest.mark.django_db
class TestAccountPagesGate:
    """The pages of no brand open only once a brand the person belongs to is in use."""

    @pytest.mark.parametrize("name", ACCOUNT_PAGES)
    def test_an_account_page_sends_someone_without_a_brand_in_use_to_contract(self, required, name):
        client, workspace = _signed_up()
        response = client.get(reverse(name))
        assert response.status_code == 302
        assert response.headers["Location"] == _url("billing:subscribe_modal", workspace)

    def test_the_organizations_screen_is_open_without_a_brand_in_use(self, required):
        """Brands are listed, created and given up on there; opening one is what its own lock answers."""
        client, workspace = _signed_up()

        assert client.get(reverse("organizations:workspaces")).status_code == 200
        assert client.get(_url("calendar:calendar", workspace)).status_code == 302

    def test_a_fragment_of_an_account_page_is_told_to_leave(self, required):
        client, workspace = _signed_up()
        response = client.get(reverse("notifications:drawer"), HTTP_HX_REQUEST="true")
        assert (response.status_code, response.headers["HX-Redirect"]) == (
            204,
            _url("billing:subscribe_modal", workspace),
        )

    @pytest.mark.parametrize(
        "name", ["accounts:settings", "billing_account:overview", "notifications:unread_count", "organizations:about"]
    )
    def test_the_essentials_stay_open(self, required, name):
        client, workspace = _signed_up()
        url = reverse(name) + (f"?workspace={workspace.id}" if name == "organizations:about" else "")
        response = client.get(url)
        assert not (
            response.status_code == 302 and response.headers["Location"] == _url("billing:subscribe_modal", workspace)
        )

    @pytest.mark.parametrize("status", ["active", "courtesy", "trial"])
    def test_account_pages_open_once_a_brand_is_in_use(self, required, status):
        client, workspace = _signed_up()
        sub = services.ensure_subscription(workspace)
        if status == "trial":
            sub.trial_ends_at = timezone.now() + datetime.timedelta(days=7)
        else:
            sub.status = status
        sub.save()
        assert client.get(reverse("members:list")).status_code == 200

    def test_staff_is_not_sent_to_contract(self, required):
        """A team account is kept in the administrative area, not asked to pay."""
        client, workspace = _signed_up()
        User.objects.filter(email="new@example.com").update(is_staff=True)

        response = client.get(reverse("members:list"))

        assert response.status_code == 302
        assert response.url == reverse("billing_staff:staff_console")

    def test_account_pages_are_open_while_the_switch_is_off(self, settings):
        settings.BILLING_REQUIRE_SUBSCRIPTION = False
        client, _workspace = _signed_up()
        assert client.get(reverse("members:list")).status_code == 200


@pytest.mark.django_db
class TestContractingForm:
    def test_the_document_is_required_before_anything_is_created(self, gateway):
        client, workspace = _signed_up()
        response = client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": "111.111.111-11"})
        assert response.headers["Location"] == _url("billing:subscribe_modal", workspace)
        assert gateway.customers == []
        assert gateway.subscriptions == []

    def test_contracting_sends_the_document_and_waits_for_payment(self, gateway):
        client, workspace = _signed_up()
        response = client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": VALID_CPF})

        assert response.headers["Location"] == _url("billing:subscribe_modal", workspace)
        assert gateway.customers[0]["cpfCnpj"] == "52998224725"
        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        assert sub.gateway_subscription_id
        assert sub.status == WorkspaceSubscription.Status.PENDING

    def test_the_document_is_asked_only_for_the_first_contract(self, gateway):
        client, workspace = _signed_up()
        client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": VALID_CPF})
        client.post(reverse("workspaces:create"), {"name": "Second"})
        second = WorkspaceMembership.objects.exclude(workspace=workspace).get().workspace

        client.post(_url("billing:subscribe_confirm", second))

        assert len(gateway.customers) == 1
        assert len(gateway.subscriptions) == 2

    def test_what_the_gateway_refused_is_shown(self, gateway):
        client, workspace = _signed_up()
        refusal = AsaasAPIError("bad", status_code=400, errors=[{"description": "O CPF informado é inválido."}])
        with mock.patch.object(gateway, "create_customer", side_effect=refusal):
            response = client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": VALID_CPF}, follow=True)
        assert "O CPF informado é inválido." in [m.message for m in response.context["messages"]]

    def test_a_refused_document_leaves_no_storage_request_behind(self, gateway):
        client, workspace = _signed_up()
        client.post(_url("billing:contract_storage", workspace), {"quantity_gb": "5", "tax_id": "123"})
        assert not WorkspaceAddon.objects.filter(workspace=workspace).exists()


@pytest.mark.django_db
class TestPayment:
    def test_the_invoice_is_where_paying_leads(self, gateway):
        client, workspace = _signed_up()
        client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": VALID_CPF})
        gateway.payments = [{"status": "PENDING", "dueDate": "2026-11-15", "invoiceUrl": "https://asaas.test/i/1"}]

        response = client.get(_url("billing:pay_invoice", workspace))

        assert response.status_code == 302
        assert response.headers["Location"] == "https://asaas.test/i/1"

    def test_no_open_invoice_returns_to_billing(self, gateway):
        client, workspace = _signed_up()
        client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": VALID_CPF})
        response = client.get(_url("billing:pay_invoice", workspace))
        assert response.headers["Location"] == _url("billing:workspace_billing", workspace)

    def test_a_member_who_cannot_spend_does_not_see_the_invoice(self, gateway):
        client, workspace = _signed_up()
        client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": VALID_CPF})
        guest, _own = _signed_up("guest@example.com")
        guest_user = User.objects.get(email="guest@example.com")
        WorkspaceMembership.objects.create(
            user=guest_user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )
        assert guest.get(_url("billing:pay_invoice", workspace)).status_code == 403

    def test_the_waiting_page_opens_the_workspace_once_paid(self, required, gateway):
        client, workspace = _signed_up()
        client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": VALID_CPF})
        poll = _url("billing:subscribe_modal", workspace)

        waiting = client.get(poll, HTTP_HX_REQUEST="true")
        assert waiting.status_code == 204
        assert "HX-Redirect" not in waiting.headers

        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        sub.status = WorkspaceSubscription.Status.ACTIVE
        sub.save(update_fields=["status"])

        paid = client.get(poll, HTTP_HX_REQUEST="true")
        assert paid.headers["HX-Redirect"] == _url("calendar:calendar", workspace)


@pytest.mark.django_db
class TestQuote:
    """The price a workspace is contracted at: list, volume, yearly."""

    def test_the_yearly_plan_charges_twelve_discounted_months_at_once(self, settings, gateway):
        settings.BILLING_WORKSPACE_PRICE_CENTS = 4790
        settings.BILLING_ANNUAL_DISCOUNT_PERCENT = 20
        client, workspace = _signed_up()

        client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": VALID_CPF, "cycle": "yearly"})

        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        assert sub.billing_cycle == "yearly"
        assert sub.price_cents == 3832
        assert gateway.subscriptions[0]["value_cents"] == 3832 * 12
        assert gateway.subscriptions[0]["cycle"] == "YEARLY"

    def test_yearly_is_refused_while_no_discount_is_configured(self, settings, gateway):
        settings.BILLING_WORKSPACE_PRICE_CENTS = 4790
        settings.BILLING_ANNUAL_DISCOUNT_PERCENT = 0
        client, workspace = _signed_up()

        client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": VALID_CPF, "cycle": "yearly"})

        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        assert sub.billing_cycle == "monthly"
        assert gateway.subscriptions[0]["value_cents"] == 4790

    def test_contracting_takes_the_list_price_of_the_day(self, settings, gateway):
        """A workspace created under an older price was never sold at it."""
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        client, workspace = _signed_up()
        services.ensure_subscription(workspace)
        settings.BILLING_WORKSPACE_PRICE_CENTS = 4790

        client.post(_url("billing:subscribe_confirm", workspace), {"tax_id": VALID_CPF})

        assert WorkspaceSubscription.objects.get(workspace=workspace).price_cents == 4790

    def test_volume_discount_reaches_the_next_workspaces_only(self, settings, gateway):
        settings.BILLING_WORKSPACE_PRICE_CENTS = 5000
        settings.BILLING_VOLUME_DISCOUNTS = ((2, 10),)
        client, first = _signed_up()
        client.post(_url("billing:subscribe_confirm", first), {"tax_id": VALID_CPF})
        client.post(reverse("workspaces:create"), {"name": "Second"})
        second = WorkspaceMembership.objects.exclude(workspace=first).get().workspace

        client.post(_url("billing:subscribe_confirm", second))

        prices = dict(WorkspaceSubscription.objects.values_list("workspace_id", "price_cents"))
        assert prices[first.id] == 5000
        assert prices[second.id] == 4500

    def test_a_comped_workspace_does_not_count_toward_volume(self, settings):
        settings.BILLING_VOLUME_DISCOUNTS = ((2, 10),)
        client, first = _signed_up()
        services.grant_courtesy(first)
        client.post(reverse("workspaces:create"), {"name": "Second"})
        second = WorkspaceMembership.objects.exclude(workspace=first).get().workspace
        assert services.quote_workspace(second)["volume_percent"] == 0


@pytest.mark.django_db
class TestFreeTrial:
    @pytest.fixture(autouse=True)
    def _trial(self, settings, required):
        settings.BILLING_TRIAL_DAYS = 7

    def _start(self, client, workspace, tax_id=VALID_CPF, phone="(11) 98888-7777"):
        return client.post(_url("billing:start_trial", workspace), {"tax_id": tax_id, "phone": phone})

    def test_the_trial_opens_the_workspace_for_seven_days(self):
        client, workspace = _signed_up()
        response = self._start(client, workspace)

        assert response.headers["Location"] == _url("billing:checkout_welcome", workspace) + "?trial=1"
        assert client.get(_url("calendar:calendar", workspace)).status_code == 200
        sub = WorkspaceSubscription.objects.get(workspace=workspace)
        remaining = sub.trial_ends_at - timezone.now()
        assert 6 < remaining.total_seconds() / 86400 <= 7

    def test_when_it_ends_the_workspace_goes_back_to_the_plan(self):
        client, workspace = _signed_up()
        self._start(client, workspace)
        WorkspaceSubscription.objects.filter(workspace=workspace).update(
            trial_ends_at=timezone.now() - datetime.timedelta(minutes=1)
        )

        response = client.get(_url("calendar:calendar", workspace))

        assert response.headers["Location"] == _url("billing:subscribe_modal", workspace)
        assert client.get(_url("billing:checkout", workspace)).context["trial_expired"] is True

    def test_it_is_not_given_without_document_and_phone(self):
        client, workspace = _signed_up()
        self._start(client, workspace, tax_id="111.111.111-11")
        self._start(client, workspace, phone="123")
        assert not _on_trial(workspace)

    def test_an_account_gets_one_trial(self):
        client, workspace = _signed_up()
        self._start(client, workspace)
        client.post(reverse("workspaces:create"), {"name": "Second"})
        second = WorkspaceMembership.objects.exclude(workspace=workspace).get().workspace

        self._start(client, second)

        assert not _on_trial(second)

    def test_the_same_document_does_not_get_a_trial_on_another_account(self):
        client, workspace = _signed_up()
        self._start(client, workspace)
        other, other_workspace = _signed_up("again@other.example")

        self._start(other, other_workspace, phone="(11) 97777-6666")

        assert not _on_trial(other_workspace)

    def test_a_viewer_cannot_start_it(self):
        _owner, workspace = _signed_up()
        guest, _own = _signed_up("guest@example.com")
        WorkspaceMembership.objects.create(
            user=User.objects.get(email="guest@example.com"),
            workspace=workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER,
        )
        assert self._start(guest, workspace, tax_id="111.444.777-35").status_code == 403

    def test_paying_during_the_trial_reuses_the_document(self, gateway):
        client, workspace = _signed_up()
        self._start(client, workspace)

        assert client.get(_url("billing:checkout", workspace)).status_code == 200
        client.post(_url("billing:subscribe_confirm", workspace))

        assert gateway.customers[0]["cpfCnpj"] == "52998224725"
        assert WorkspaceSubscription.objects.get(workspace=workspace).gateway_subscription_id

    def test_no_trial_is_offered_while_the_setting_is_zero(self, settings):
        settings.BILLING_TRIAL_DAYS = 0
        client, workspace = _signed_up()
        self._start(client, workspace)
        assert not _on_trial(workspace)


@pytest.mark.django_db
class TestTrialOncePerPerson:
    """A second account does not get a trial by changing one detail."""

    OTHER_CPF = "111.444.777-35"

    @pytest.fixture(autouse=True)
    def _trial(self, settings):
        settings.BILLING_TRIAL_DAYS = 7

    def _start(self, email, *, tax_id, phone):
        client, workspace = _signed_up(email)
        client.post(_url("billing:start_trial", workspace), {"tax_id": tax_id, "phone": phone})
        return _on_trial(workspace)

    def test_the_same_phone_is_refused(self):
        assert self._start("ana@gmail.com", tax_id=VALID_CPF, phone="(11) 98888-7777")
        assert not self._start("bia@hotmail.com", tax_id=self.OTHER_CPF, phone="11 98888 7777")

    def test_another_address_at_the_same_company_domain_is_refused(self):
        assert self._start("ana@agenciasol.com.br", tax_id=VALID_CPF, phone="(11) 98888-7777")
        assert not self._start("contato@agenciasol.com.br", tax_id=self.OTHER_CPF, phone="(11) 97777-6666")

    def test_two_people_at_a_free_mailbox_provider_both_get_one(self):
        assert self._start("ana@gmail.com", tax_id=VALID_CPF, phone="(11) 98888-7777")
        assert self._start("bia@gmail.com", tax_id=self.OTHER_CPF, phone="(11) 97777-6666")

    def test_an_alias_of_the_same_inbox_is_the_same_email(self):
        assert services.normalize_trial_email("Ana.Silva+teste@GoogleMail.com") == "anasilva@gmail.com"
        assert services.normalize_trial_email("ana+2@agenciasol.com.br") == "ana@agenciasol.com.br"


def _activated(email="active@example.com"):
    """An account already in use: its first brand is exempt, and nobody answered the about step."""
    client, workspace = _signed_up(email)
    services.grant_courtesy(workspace)
    return client, workspace


def _create_brand(client, organization, name="Second brand"):
    response = client.post(reverse("workspaces:create"), {"name": name, "organization": str(organization.id)})
    return response, Workspace.objects.get(organization=organization, name=name)


@pytest.mark.django_db
class TestAnotherBrandInAnActivatedAccount:
    """An account with a brand in use adds a brand without being set up again,
    and can give up on it without paying or starting a trial."""

    @pytest.fixture(autouse=True)
    def _locked_with_trial(self, settings, required):
        settings.BILLING_TRIAL_DAYS = 7

    def test_the_new_brand_goes_to_its_plan_not_to_the_company_step(self):
        client, first = _activated()

        response, brand = _create_brand(client, first.organization)
        assert response.headers["Location"] == _url("billing:subscribe_modal", brand)
        checkout = client.get(_url("billing:checkout", brand))

        assert checkout.status_code == 200
        assert checkout.context["in_setup"] is False
        assert checkout.context["just_created"] is True

    def test_a_new_account_is_still_asked_about_its_business(self):
        client, workspace = _signed_up()

        response = client.get(_url("billing:checkout", workspace))

        assert response.status_code == 302
        assert reverse("organizations:about") in response.headers["Location"]

    def test_a_new_account_can_leave_its_checkout_and_keeps_its_brand(self):
        """Nobody has to sign out to give up: the brand stays, locked, for later."""
        client, workspace = _signed_up()
        _answer_about(client, workspace)
        assert client.get(_url("billing:checkout", workspace)).status_code == 200

        response = client.post(_url("billing:checkout_cancel", workspace))

        assert response.headers["Location"] == reverse("organizations:workspaces")
        assert Workspace.objects.filter(pk=workspace.pk).exists()
        assert services.ensure_billing_profile(workspace.organization).trial_started_at is None
        assert client.get(reverse("organizations:workspaces")).status_code == 200

    def test_cancelling_discards_the_brand_and_starts_no_trial(self):
        client, first = _activated()
        _response, brand = _create_brand(client, first.organization)

        response = client.post(_url("billing:checkout_cancel", brand))

        assert response.headers["Location"] == reverse("organizations:workspaces")
        assert not Workspace.objects.filter(pk=brand.pk).exists()
        profile = services.ensure_billing_profile(first.organization)
        assert profile.trial_started_at is None
        assert client.get(reverse("organizations:workspaces")).status_code == 200

    def test_after_cancelling_home_is_the_brand_in_use(self):
        client, first = _activated()
        _response, brand = _create_brand(client, first.organization)
        client.post(_url("billing:checkout_cancel", brand))

        response = client.get("/")

        assert response.headers["Location"] == _url("workspaces:dashboard", first)
        assert client.get(_url("workspaces:dashboard", first)).status_code == 200

    def test_the_brand_in_use_opens_straight_away(self):
        """No company data and no plan in the way of a brand already activated."""
        client, first = _activated()

        assert client.get(_url("calendar:calendar", first)).status_code == 200

    def test_a_brand_left_unpaid_stays_locked_on_its_own(self):
        client, first = _activated()
        _response, brand = _create_brand(client, first.organization)

        assert client.get(_url("calendar:calendar", brand)).status_code == 302
        assert client.get(_url("calendar:calendar", first)).status_code == 200

    def test_leaving_an_older_brand_s_checkout_keeps_the_brand(self):
        """Only a brand created just now is given up on by leaving."""
        client, first = _activated()
        _response, brand = _create_brand(client, first.organization)
        session = client.session
        session.pop("new_brand_id")
        session.save()

        client.post(_url("billing:checkout_cancel", brand))

        assert Workspace.objects.filter(pk=brand.pk).exists()

    def test_a_brand_with_content_is_never_discarded(self):
        client, first = _activated()
        _response, brand = _create_brand(client, first.organization)
        Post.objects.create(workspace=brand, author=User.objects.get(email="active@example.com"), caption="Kept")

        client.post(_url("billing:checkout_cancel", brand))

        assert Workspace.objects.filter(pk=brand.pk).exists()

    def test_the_only_brand_of_an_organization_is_never_discarded(self):
        client, first = _activated()
        _response, brand = _create_brand(client, first.organization)
        first.delete()

        client.post(_url("billing:checkout_cancel", brand))

        assert Workspace.objects.filter(pk=brand.pk).exists()

    def test_someone_else_cannot_cancel_it(self):
        client, first = _activated()
        _response, brand = _create_brand(client, first.organization)
        stranger, _theirs = _signed_up("stranger@example.com")

        response = stranger.post(_url("billing:checkout_cancel", brand))

        assert response.status_code == 403
        assert Workspace.objects.filter(pk=brand.pk).exists()

    def test_get_does_not_cancel(self):
        client, first = _activated()
        _response, brand = _create_brand(client, first.organization)

        assert client.get(_url("billing:checkout_cancel", brand)).status_code == 405
        assert Workspace.objects.filter(pk=brand.pk).exists()
