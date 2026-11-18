"""Meta Ads: what the client sends and reads, the ad-account boundary, and who may do what.

Only the HTTP boundary is faked (``MetaAdsClient._request``): a small router
answers like the Graph API for the paths each test needs.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest import mock

import httpx
import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.ads import services
from apps.ads.models import AdsConnection
from apps.members.models import OrgMembership, WorkspaceMembership
from providers import meta_ads
from providers.exceptions import TokenExpiredError

ACCOUNT = "act_111"
OTHER_ACCOUNT = "act_999"


class FakeGraph:
    """Answers ``_request`` from a ``{(METHOD, path): body}`` table and records every call."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, client, method, url, **kwargs):
        path = url.split("/v25.0/", 1)[1]
        self.calls.append((method, path, kwargs.get("data") or kwargs.get("params") or {}))
        body = self.routes.get((method, path))
        if body is None:
            raise AssertionError(f"unexpected Graph call {method} {path}")
        if isinstance(body, Exception):
            raise body
        return httpx.Response(200, json=body, request=httpx.Request(method, url))

    def posted(self, path: str) -> dict:
        return next(data for method, called, data in self.calls if method == "POST" and called == path)


def fake_graph(routes):
    graph = FakeGraph(routes)
    return graph, mock.patch.object(meta_ads.MetaAdsClient, "_request", autospec=True, side_effect=graph)


@pytest.fixture(autouse=True)
def _meta_app(settings):
    settings.BILLING_REQUIRE_SUBSCRIPTION = False
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {
        **settings.PLATFORM_CREDENTIALS_FROM_ENV,
        "facebook": {"app_id": "app-1", "app_secret": "secret-1"},
    }
    settings.PLATFORM_META_ADS_CONFIG_ID = ""


@pytest.fixture
def workspace(organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Ads WS", organization=organization)


def _member(workspace, email, role):
    from apps.accounts.models import User

    member = User.objects.create_user(email=email, password="testpass123", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(
        user=member, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER
    )
    WorkspaceMembership.objects.create(user=member, workspace=workspace, workspace_role=role)
    return member


@pytest.fixture
def manager(workspace):
    return _member(workspace, "ads-manager@example.com", WorkspaceMembership.WorkspaceRole.MANAGER)


@pytest.fixture
def viewer(workspace):
    return _member(workspace, "ads-viewer@example.com", WorkspaceMembership.WorkspaceRole.VIEWER)


@pytest.fixture
def connection(workspace, manager):
    return AdsConnection.objects.create(
        workspace=workspace,
        access_token="user-token",
        ad_account_id=ACCOUNT,
        ad_account_name="Loja",
        currency="BRL",
        pixel_id="px-1",
        pixel_name="Pixel da loja",
        page_id="page-1",
        page_name="Loja Page",
        status=AdsConnection.Status.CONNECTED,
        connected_by=manager,
    )


def _logged_in(user):
    client = Client()
    client.force_login(user)
    return client


def _new_campaign(**overrides):
    values = dict(
        name="Black Friday",
        objective="OUTCOME_SALES",
        daily_budget=Decimal("25.50"),
        start_time=None,
        end_time=None,
        countries=["BR"],
        age_min=25,
        age_max=45,
        genders=[2],
        image_bytes=b"\x89PNG fake",
        message="Até 50% off",
        headline="Só hoje",
        link="https://loja.example.com/bf",
        call_to_action="SHOP_NOW",
    )
    values.update(overrides)
    return services.NewCampaign(**values)


CREATION_ROUTES = {
    ("POST", f"{ACCOUNT}/campaigns"): {"id": "c-1"},
    ("POST", f"{ACCOUNT}/adsets"): {"id": "as-1"},
    ("POST", f"{ACCOUNT}/adimages"): {"images": {"bytes": {"hash": "img-hash"}}},
    ("POST", f"{ACCOUNT}/adcreatives"): {"id": "cr-1"},
    ("POST", f"{ACCOUNT}/ads"): {"id": "ad-1"},
}


# ------------------------------------------------------------------
# Client
# ------------------------------------------------------------------


class TestClientParsing:
    def test_a_purchase_reported_under_several_names_counts_once(self):
        row = meta_ads.parse_insights_row(
            {
                "spend": "120.40",
                "impressions": "1000",
                "actions": [
                    {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "3"},
                    {"action_type": "omni_purchase", "value": "3"},
                    {"action_type": "purchase", "value": "3"},
                ],
                "action_values": [{"action_type": "offsite_conversion.fb_pixel_purchase", "value": "450.00"}],
            }
        )
        assert row.purchases == 3
        assert row.purchase_value == Decimal("450.00")
        assert row.spend == Decimal("120.40")

    def test_budgets_travel_in_cents_except_zero_decimal_currencies(self):
        assert meta_ads.to_minor_units(Decimal("25.50"), "BRL") == 2550
        assert meta_ads.to_minor_units(Decimal("1000"), "JPY") == 1000
        assert meta_ads.from_minor_units("2550", "BRL") == Decimal("25.50")
        assert meta_ads.from_minor_units("0", "BRL") is None

    def test_an_expired_token_is_classified_and_meta_sentence_kept(self):
        client = meta_ads.MetaAdsClient({"app_id": "a", "app_secret": "s"})
        body = {"error": {"code": 190, "message": "Error validating access token"}}
        exc = client._error_for_response(httpx.Response(400, json=body))
        assert isinstance(exc, TokenExpiredError)

        body = {"error": {"code": 100, "error_user_title": "Orçamento baixo", "error_user_msg": "Mínimo R$ 5,00"}}
        exc = client._error_for_response(httpx.Response(400, json=body))
        assert "Orçamento baixo: Mínimo R$ 5,00" in str(exc)
        assert not exc.retryable


# ------------------------------------------------------------------
# The ad-account boundary
# ------------------------------------------------------------------


@pytest.mark.django_db
class TestAdAccountBoundary:
    def test_a_campaign_of_another_ad_account_is_refused(self, connection):
        graph, patch = fake_graph({("GET", "555"): {"id": "555", "name": "x", "account_id": "999"}})
        with patch, pytest.raises(services.AdsError):
            services.get_campaign(connection, "555")

    def test_status_change_never_reaches_another_account_campaign(self, connection):
        graph, patch = fake_graph({("GET", "555"): {"id": "555", "name": "x", "account_id": "999"}})
        with patch, pytest.raises(services.AdsError):
            services.set_campaign_status(connection, "555", "ACTIVE")
        assert not [call for call in graph.calls if call[0] == "POST"]

    def test_a_non_numeric_campaign_id_is_refused_without_calling_meta(self, connection):
        graph, patch = fake_graph({})
        with patch, pytest.raises(services.AdsError):
            services.get_campaign(connection, "act_999")
        assert graph.calls == []

    def test_choosing_an_account_the_token_does_not_reach_is_refused(self, connection):
        routes = {
            ("GET", "me/adaccounts"): {"data": [{"id": ACCOUNT, "name": "Loja", "currency": "BRL"}]},
            ("GET", f"{ACCOUNT}/adspixels"): {"data": [{"id": "px-1", "name": "Pixel"}]},
            ("GET", "me/accounts"): {"data": [{"id": "page-1", "name": "Loja Page"}]},
        }
        graph, patch = fake_graph(routes)
        with patch, pytest.raises(services.AdsError):
            services.choose_assets(connection, ad_account_id=OTHER_ACCOUNT, pixel_id="", page_id="")
        with patch, pytest.raises(services.AdsError):
            services.choose_assets(connection, ad_account_id=ACCOUNT, pixel_id="px-other", page_id="")
        connection.refresh_from_db()
        assert connection.pixel_id == "px-1"

    def test_an_expired_token_marks_the_connection(self, connection):
        expired = TokenExpiredError("Meta Ads: token expired", status_code=400)
        graph, patch = fake_graph({("GET", f"{ACCOUNT}/campaigns"): expired})
        with patch, pytest.raises(services.AdsError):
            services.list_campaigns(connection)
        connection.refresh_from_db()
        assert connection.status == AdsConnection.Status.ERROR


# ------------------------------------------------------------------
# Creation
# ------------------------------------------------------------------


@pytest.mark.django_db
class TestCampaignCreation:
    def test_the_campaign_is_paused_and_the_ad_set_optimises_for_the_pixel(self, connection):
        graph, patch = fake_graph(CREATION_ROUTES)
        with patch:
            assert services.create_campaign(connection, _new_campaign()) == "c-1"

        campaign = graph.posted(f"{ACCOUNT}/campaigns")
        assert campaign["status"] == "PAUSED"
        assert campaign["objective"] == "OUTCOME_SALES"
        assert campaign["is_adset_budget_sharing_enabled"] == "false"

        ad_set = graph.posted(f"{ACCOUNT}/adsets")
        assert ad_set["daily_budget"] == 2550
        assert ad_set["optimization_goal"] == "OFFSITE_CONVERSIONS"
        assert json.loads(ad_set["promoted_object"]) == {"pixel_id": "px-1", "custom_event_type": "PURCHASE"}
        targeting = json.loads(ad_set["targeting"])
        assert targeting["geo_locations"] == {"countries": ["BR"]}
        assert targeting["genders"] == [2]
        assert targeting["targeting_automation"] == {"advantage_audience": 0}

        story = json.loads(graph.posted(f"{ACCOUNT}/adcreatives")["object_story_spec"])
        assert story["page_id"] == "page-1"
        assert story["link_data"]["image_hash"] == "img-hash"
        assert story["link_data"]["call_to_action"]["type"] == "SHOP_NOW"
        assert json.loads(graph.posted(f"{ACCOUNT}/ads")["creative"]) == {"creative_id": "cr-1"}

    def test_a_refused_creative_deletes_the_campaign_again(self, connection):
        from providers.exceptions import APIError

        routes = {
            **CREATION_ROUTES,
            ("POST", f"{ACCOUNT}/adcreatives"): APIError("Meta Ads: imagem inválida", status_code=400),
            ("DELETE", "c-1"): {"success": True},
        }
        graph, patch = fake_graph(routes)
        with patch, pytest.raises(services.AdsError):
            services.create_campaign(connection, _new_campaign())
        assert ("DELETE", "c-1", {}) in graph.calls
        assert not [call for call in graph.calls if call[1] == f"{ACCOUNT}/ads"]

    def test_a_sales_campaign_without_pixel_is_refused_before_spending_anything(self, connection):
        connection.pixel_id = ""
        connection.save()
        graph, patch = fake_graph(CREATION_ROUTES)
        with patch, pytest.raises(services.AdsError):
            services.create_campaign(connection, _new_campaign())
        assert graph.calls == []


# ------------------------------------------------------------------
# Who may do what
# ------------------------------------------------------------------


@pytest.mark.django_db
class TestPermissions:
    def test_a_viewer_can_read_but_not_change(self, workspace, viewer, connection):
        client = _logged_in(viewer)
        routes = {
            ("GET", f"{ACCOUNT}/insights"): {"data": []},
            ("GET", f"{ACCOUNT}/campaigns"): {"data": []},
        }
        graph, patch = fake_graph(routes)
        with patch:
            assert client.get(reverse("ads:index", kwargs={"workspace_id": workspace.id})).status_code == 200
        kwargs = {"workspace_id": workspace.id}
        assert client.post(reverse("ads:connect", kwargs=kwargs)).status_code == 403
        assert client.get(reverse("ads:campaign_new", kwargs=kwargs)).status_code == 403
        assert client.post(reverse("ads:disconnect", kwargs=kwargs)).status_code == 403
        status_url = reverse("ads:campaign_status", kwargs={**kwargs, "campaign_id": "123"})
        assert client.post(status_url, {"status": "ACTIVE"}).status_code == 403
        assert AdsConnection.objects.filter(pk=connection.pk).exists()

    def test_another_workspace_member_never_sees_this_connection(self, connection, organization):
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(name="Other", organization=organization)
        outsider = _member(other, "ads-outsider@example.com", WorkspaceMembership.WorkspaceRole.OWNER)
        client = _logged_in(outsider)
        response = client.get(reverse("ads:index", kwargs={"workspace_id": other.id}))
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("social_accounts:list", kwargs={"workspace_id": other.id}))
        assert client.get(reverse("ads:index", kwargs={"workspace_id": connection.workspace_id})).status_code in (
            302,
            403,
            404,
        )

    def test_connect_sends_the_ads_permissions_to_meta(self, workspace, manager):
        client = _logged_in(manager)
        response = client.post(reverse("ads:connect", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 302
        assert "facebook.com" in response["Location"]
        assert "ads_management" in response["Location"]
        assert "/ads/meta/callback/" in response["Location"].replace("%2F", "/")

    def test_a_callback_without_the_session_nonce_keeps_nothing(self, workspace, manager):
        from apps.social_accounts.views import _sign_state

        client = _logged_in(manager)
        state = _sign_state(workspace.id, "meta_ads", manager.id, "forged")
        with mock.patch.object(services, "finish_sign_in") as finish:
            response = client.get(reverse("ads_callback:meta_callback"), {"code": "c", "state": state})
        assert response.status_code == 302
        finish.assert_not_called()
        assert not AdsConnection.objects.filter(workspace=workspace).exists()

    def test_a_social_accounts_state_is_not_accepted_here(self, workspace, manager):
        from apps.social_accounts.views import _sign_state

        client = _logged_in(manager)
        client.post(reverse("ads:connect", kwargs={"workspace_id": workspace.id}))
        nonce = client.session["ads_oauth"]["nonce"]
        state = _sign_state(workspace.id, "facebook", manager.id, nonce)
        with mock.patch.object(services, "finish_sign_in") as finish:
            client.get(reverse("ads_callback:meta_callback"), {"code": "c", "state": state})
        finish.assert_not_called()


@pytest.mark.django_db
class TestSocialAccountsPage:
    def test_meta_ads_is_offered_to_connect(self, workspace, manager):
        response = _logged_in(manager).get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        assert reverse("ads:connect", kwargs={"workspace_id": workspace.id}) in response.content.decode()

    def test_a_connected_ad_account_shows_with_the_accounts(self, workspace, manager, connection):
        response = _logged_in(manager).get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id}))
        page = response.content.decode()
        assert reverse("ads:index", kwargs={"workspace_id": workspace.id}) in page
        assert "Loja" in page
        assert response.context["stats"]["connected"] == 1

    def test_connect_opens_the_guide_which_posts_into_the_popup(self, workspace, manager):
        page = _logged_in(manager).get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id}))
        html = page.content.decode()
        assert "guide = 'meta_ads'" in html
        assert "connect-guide-title-meta_ads" in html
        connect_url = reverse("ads:connect", kwargs={"workspace_id": workspace.id})
        assert f'action="{connect_url}" data-oauth-popup' in html

    def test_the_sign_in_in_a_popup_ends_on_the_page_that_hands_over(self, workspace, manager):
        client = _logged_in(manager)
        client.post(reverse("ads:connect", kwargs={"workspace_id": workspace.id}), {"popup": "1"})
        nonce = client.session["ads_oauth"]["nonce"]
        from apps.social_accounts.views import _sign_state

        state = _sign_state(workspace.id, "meta_ads", manager.id, nonce)
        with mock.patch.object(services, "finish_sign_in"):
            response = client.get(reverse("ads_callback:meta_callback"), {"code": "c", "state": state})
        assert response.status_code == 200
        assert "social_accounts/connect_done.html" in [t.name for t in response.templates]
        assert response.context["next_url"] == reverse("ads:settings", kwargs={"workspace_id": workspace.id})


@pytest.mark.django_db
class TestAdsOnlyAfterConnecting:
    def _sidebar_link(self, client, workspace):
        page = client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})).content.decode()
        return (
            f'href="{reverse("ads:index", kwargs={"workspace_id": workspace.id})}" class="mb-0.5 sidebar-nav-item'
            in page
        )

    def test_the_ads_entry_is_hidden_until_connected(self, workspace, manager):
        assert not self._sidebar_link(_logged_in(manager), workspace)

    def test_the_ads_entry_shows_once_an_ad_account_is_chosen(self, workspace, manager, connection):
        assert self._sidebar_link(_logged_in(manager), workspace)


@pytest.mark.django_db
class TestReports:
    REPORT_ROUTES = {
        ("GET", f"{ACCOUNT}/insights"): {
            "data": [{"date_start": "2026-11-17", "spend": "40.00", "impressions": "900", "inline_link_clicks": "30"}]
        },
        ("GET", f"{ACCOUNT}/campaigns"): {
            "data": [{"id": "c-1", "name": "Black Friday", "status": "ACTIVE", "effective_status": "ACTIVE"}]
        },
    }

    def test_meta_ads_appears_in_the_reports(self, workspace, viewer, connection):
        graph, patch = fake_graph(self.REPORT_ROUTES)
        with patch:
            response = _logged_in(viewer).get(reverse("analytics:meta_ads", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        assert response.context["active_ads"] == connection
        assert [card["metric"] for card in response.context["hero_cards"]] == [
            "spend",
            "impressions",
            "link_clicks",
            "results",
        ]

    def test_no_report_without_a_connection(self, workspace, viewer):
        response = _logged_in(viewer).get(reverse("analytics:meta_ads", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 404
