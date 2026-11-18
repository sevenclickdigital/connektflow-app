"""Meta Ads: what the client sends and reads, the ad-account boundary, and who may do what.

Only the HTTP boundary is faked (``MetaAdsClient._request``): a small router
answers like the Graph API for the paths each test needs.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
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
            ("GET", f"{ACCOUNT}/promote_pages"): {"data": [{"id": "page-1", "name": "Loja Page"}]},
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


@pytest.mark.django_db
class TestSettingsPage:
    ROUTES = {
        ("GET", "me/adaccounts"): {
            "data": [
                {"id": ACCOUNT, "name": "Loja", "currency": "BRL", "account_status": 1},
                {"id": OTHER_ACCOUNT, "name": "Outra marca", "currency": "USD", "account_status": 1},
            ]
        },
        ("GET", f"{ACCOUNT}/adspixels"): {
            "data": [{"id": "px-1", "name": "Pixel da loja", "last_fired_time": "2026-11-17T12:00:00+0000"}]
        },
        ("GET", f"{OTHER_ACCOUNT}/adspixels"): {"data": [{"id": "px-9", "name": "Pixel da outra"}]},
        ("GET", f"{ACCOUNT}/promote_pages"): {"data": [{"id": "page-1", "name": "Loja Page"}]},
        ("GET", f"{OTHER_ACCOUNT}/promote_pages"): {"data": [{"id": "page-9", "name": "Outra Page"}]},
    }

    def test_the_page_shows_before_the_lists_load(self, connection, manager):
        graph, patch = fake_graph(self.ROUTES)
        url = reverse("ads:settings", kwargs={"workspace_id": connection.workspace_id})
        with patch:
            response = _logged_in(manager).get(url, {"account": OTHER_ACCOUNT})
        assert graph.calls == []
        assert f"account={OTHER_ACCOUNT}&amp;partial=body" in response.content.decode()

    def test_lists_every_ad_account_with_the_chosen_one_marked(self, connection, manager):
        graph, patch = fake_graph(self.ROUTES)
        with patch:
            response = _logged_in(manager).get(
                reverse("ads:settings", kwargs={"workspace_id": connection.workspace_id}),
                {"partial": "body"},
                HTTP_HX_REQUEST="true",
            )
        assert response.status_code == 200
        body = response.content.decode()
        assert "Outra marca" in body
        assert f'href="?account={ACCOUNT}"' in body
        assert response.context["choices"].account.id == ACCOUNT
        assert response.context["form"]["pixel_id"].value() == "px-1"

    def test_picking_another_account_shows_only_its_pixels_and_pages(self, connection, manager):
        graph, patch = fake_graph(self.ROUTES)
        url = reverse("ads:settings", kwargs={"workspace_id": connection.workspace_id})
        with patch:
            response = _logged_in(manager).get(
                url, {"account": OTHER_ACCOUNT, "partial": "body"}, HTTP_HX_REQUEST="true"
            )
        assert [p.id for p in response.context["choices"].pixels] == ["px-9"]
        assert [p.id for p in response.context["choices"].pages] == ["page-9"]
        body = response.content.decode()
        assert "Pixel da loja" not in body
        assert "Loja Page" not in body
        # Nothing of the account in use is carried over to the one being looked at.
        assert response.context["form"]["pixel_id"].value() in ("", "px-9")

    def test_a_page_of_another_ad_account_is_refused(self, connection, manager):
        graph, patch = fake_graph(self.ROUTES)
        with patch, pytest.raises(services.AdsError):
            services.choose_assets(connection, ad_account_id=OTHER_ACCOUNT, pixel_id="px-9", page_id="page-1")
        connection.refresh_from_db()
        assert (connection.ad_account_id, connection.page_id) == (ACCOUNT, "page-1")

    def test_a_fresh_connection_starts_with_its_only_page_chosen(self, connection, manager):
        connection.page_id = ""
        connection.status = AdsConnection.Status.PENDING
        connection.save()
        graph, patch = fake_graph(self.ROUTES)
        with patch:
            response = _logged_in(manager).get(
                reverse("ads:settings", kwargs={"workspace_id": connection.workspace_id}),
                {"partial": "body"},
                HTTP_HX_REQUEST="true",
            )
        assert response.context["form"]["page_id"].value() == "page-1"


# ------------------------------------------------------------------
# Trends and where the results come from
# ------------------------------------------------------------------


def _row(**values):
    return meta_ads.Insights(**values)


class TestTrends:
    def test_a_cheaper_click_is_good_news_and_more_spend_is_neither(self):
        now, before = _row(cpc=Decimal("0.80"), spend=Decimal(200)), _row(cpc=Decimal("1.00"), spend=Decimal(100))
        assert services.compare(now, before, "cpc") == services.Change(-20.0, "good")
        assert services.compare(now, before, "spend") == services.Change(100.0, "neutral")
        assert services.compare(_row(ctr=Decimal(1)), _row(ctr=Decimal(2)), "ctr") == services.Change(-50.0, "bad")

    def test_a_number_the_previous_window_did_not_have_still_gets_a_neutral_change(self):
        assert services.compare(_row(roas=Decimal(2)), _row(), "roas") == services.Change(100.0, "neutral", True)
        assert services.compare(_row(purchases=0), _row(purchases=0), "purchases") == services.Change(
            0.0, "neutral", True
        )

    def test_no_value_now_gives_no_change(self):
        assert (
            services.compare(_row(leads=0, spend=Decimal(10)), _row(leads=2, spend=Decimal(10)), "cost_per_lead")
            is None
        )
        assert services.compare(_row(), _row(roas=Decimal(3)), "roas") is None

    def test_only_an_https_preview_link_reaches_the_page(self):
        assert services._https_url("https://fb.me/adspreview/abc") == "https://fb.me/adspreview/abc"
        assert services._https_url("javascript:alert(1)") == ""
        assert services._https_url(None) == ""

    def test_the_previous_window_ends_the_day_before_the_current_one_starts(self):
        since, _until = services.date_window(7)
        before_since, before_until = services.previous_window(7)
        assert date.fromisoformat(since) - date.fromisoformat(before_until) == timedelta(days=1)
        assert date.fromisoformat(before_until) - date.fromisoformat(before_since) == timedelta(days=6)

    def test_picked_dates_ask_meta_for_those_days_and_the_ones_before(self):
        end = date(2026, 10, 15)
        assert services.date_window(15, end) == ("2026-10-01", "2026-10-15")
        assert services.previous_window(15, end) == ("2026-09-16", "2026-09-30")

    def test_a_sales_campaign_pays_back_only_when_roas_reaches_one(self):
        campaign = services._campaign_from_row({"id": "1", "objective": "OUTCOME_SALES"}, "BRL")
        campaign.insights = _row(roas=Decimal("0.9"))
        assert campaign.pays_back is False
        campaign.insights = _row(roas=Decimal("1.0"))
        assert campaign.pays_back is True
        traffic = services._campaign_from_row({"id": "2", "objective": "OUTCOME_TRAFFIC"}, "BRL")
        traffic.insights = _row(roas=Decimal(3))
        assert traffic.pays_back is None
        assert traffic.key_metric == "cpc"


@pytest.mark.django_db
class TestBreakdowns:
    def test_the_best_slice_needs_a_tenth_of_the_spend(self, connection):
        campaign = services._campaign_from_row({"id": "123", "objective": "OUTCOME_TRAFFIC"}, "BRL")
        campaign.insights = _row(spend=Decimal(100))
        placement = "publisher_platform,platform_position"
        rows = {
            # Cheapest click, but too little spend to count.
            "tiny": {"spend": "2", "cpc": "0.10", "publisher_platform": "instagram", "platform_position": "reels"},
            "good": {"spend": "50", "cpc": "0.50", "publisher_platform": "instagram", "platform_position": "story"},
            "dear": {"spend": "48", "cpc": "1.20", "publisher_platform": "facebook", "platform_position": "feed"},
        }

        def answer(client, method, url, **kwargs):
            params = kwargs.get("params") or {}
            if params.get("breakdowns") == placement:
                data = list(rows.values())
            elif params.get("level") == "ad":
                data = [{"spend": "100", "cpc": "0.7", "ad_id": "ad-1", "ad_name": "Vídeo"}]
            else:
                data = [{"spend": "100", "age": "25-34", "gender": "female"}]
            return httpx.Response(200, json={"data": data}, request=httpx.Request(method, url))

        with mock.patch.object(meta_ads.MetaAdsClient, "_request", autospec=True, side_effect=answer):
            result = services.campaign_breakdowns(
                connection, campaign, [{"id": "ad-1", "thumbnail_url": "https://x/t.jpg"}], days=7
            )

        best = [row.label for row in result["placements"] if row.is_best]
        assert best == ["Instagram · Stories"]
        assert [row.insights.spend for row in result["placements"]] == [Decimal(50), Decimal(48), Decimal(2)]
        assert result["ads"][0].thumbnail_url == "https://x/t.jpg"
        assert result["audience"][0].label.startswith("25-34 · ")
        assert result["key_metric"] == "cpc"


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

    def test_the_loaded_parts_are_closed_to_another_workspace(self, connection, organization):
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(name="Other", organization=organization)
        outsider = _member(other, "ads-outsider2@example.com", WorkspaceMembership.WorkspaceRole.OWNER)
        client = _logged_in(outsider)
        graph, patch = fake_graph({})
        kwargs = {"workspace_id": connection.workspace_id}
        with patch:
            assert client.get(reverse("ads:overview", kwargs=kwargs)).status_code in (302, 403, 404)
            body_url = reverse("ads:campaign_body", kwargs={**kwargs, "campaign_id": "555"})
            assert client.get(body_url).status_code in (302, 403, 404)
        assert graph.calls == []

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
        url = reverse("analytics:meta_ads", kwargs={"workspace_id": workspace.id})
        client = _logged_in(viewer)
        graph, patch = fake_graph(self.REPORT_ROUTES)
        with patch:
            page = client.get(url)
            assert graph.calls == []
            response = client.get(url, {"partial": "body"}, HTTP_HX_REQUEST="true")
        assert page.status_code == 200
        assert "partial=body" in page.content.decode()
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


@pytest.mark.django_db
class TestLoadingFromMeta:
    def test_the_page_shows_without_waiting_on_meta(self, workspace, viewer, connection):
        graph, patch = fake_graph({})
        with patch:
            response = _logged_in(viewer).get(reverse("ads:index", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        assert graph.calls == []
        assert reverse("ads:overview", kwargs={"workspace_id": workspace.id}) in response.content.decode()

    def test_the_overview_brings_the_campaigns(self, workspace, viewer, connection):
        routes = {
            ("GET", f"{ACCOUNT}/insights"): {"data": [{"spend": "10", "campaign_id": "777"}]},
            ("GET", f"{ACCOUNT}/campaigns"): {"data": [{"id": "777", "name": "Black Friday", "status": "ACTIVE"}]},
        }
        graph, patch = fake_graph(routes)
        with patch:
            response = _logged_in(viewer).get(reverse("ads:overview", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        assert "Black Friday" in response.content.decode()

    def test_the_campaign_body_refuses_a_campaign_of_another_ad_account(self, workspace, viewer, connection):
        graph, patch = fake_graph({("GET", "555"): {"id": "555", "name": "x", "account_id": "999"}})
        url = reverse("ads:campaign_body", kwargs={"workspace_id": workspace.id, "campaign_id": "555"})
        with patch:
            response = _logged_in(viewer).get(url)
        assert response.status_code == 200
        assert "alert-error" in response.content.decode()
        assert [path for _method, path, _data in graph.calls] == ["555"]


@pytest.mark.django_db
class TestEditDialog:
    def test_a_manager_gets_the_edit_dialog_in_place_of_pause(self, workspace, manager, connection):
        graph, patch = fake_graph(
            {("GET", "777"): {"id": "777", "name": "Natal", "status": "ACTIVE", "account_id": "111"}}
        )
        url = reverse("ads:campaign_detail", kwargs={"workspace_id": workspace.id, "campaign_id": "777"})
        with patch:
            body = _logged_in(manager).get(url).content.decode()
        assert 'aria-labelledby="campaign-edit-title"' in body
        assert reverse("ads:campaign_ad_sets", kwargs={"workspace_id": workspace.id, "campaign_id": "777"}) in body
        assert [path for _method, path, _data in graph.calls] == ["777"]

    def test_a_viewer_neither_sees_nor_loads_the_dialog(self, workspace, viewer, connection):
        graph, patch = fake_graph(
            {("GET", "777"): {"id": "777", "name": "Natal", "status": "ACTIVE", "account_id": "111"}}
        )
        kwargs = {"workspace_id": workspace.id, "campaign_id": "777"}
        client = _logged_in(viewer)
        with patch:
            body = client.get(reverse("ads:campaign_detail", kwargs=kwargs)).content.decode()
            assert client.get(reverse("ads:campaign_ad_sets", kwargs=kwargs)).status_code == 403
        assert "campaign-edit-title" not in body

    def test_the_ad_sets_of_another_ad_account_campaign_are_never_read(self, workspace, manager, connection):
        graph, patch = fake_graph({("GET", "555"): {"id": "555", "name": "x", "account_id": "999"}})
        url = reverse("ads:campaign_ad_sets", kwargs={"workspace_id": workspace.id, "campaign_id": "555"})
        with patch:
            response = _logged_in(manager).get(url)
        assert "alert-error" in response.content.decode()
        assert [path for _method, path, _data in graph.calls] == ["555"]

    def test_the_dialog_lists_each_ad_set_with_its_budget_form(self, workspace, manager, connection):
        routes = {
            ("GET", "777"): {"id": "777", "name": "Natal", "status": "ACTIVE", "account_id": "111"},
            ("GET", "777/adsets"): {"data": [{"id": "s1", "name": "Mães 25-44", "daily_budget": "5000"}]},
        }
        graph, patch = fake_graph(routes)
        url = reverse("ads:campaign_ad_sets", kwargs={"workspace_id": workspace.id, "campaign_id": "777"})
        with patch:
            body = _logged_in(manager).get(url).content.decode()
        assert "Mães 25-44" in body
        assert (
            reverse("ads:ad_set_edit", kwargs={"workspace_id": workspace.id, "campaign_id": "777", "ad_set_id": "s1"})
            in body
        )
