"""Pinterest Ads: what the client sends and reads, the ad-account boundary, and who may do what.

Only the HTTP boundary is faked (``PinterestAdsClient._request``): a small
router answers like the Pinterest API for the paths each test needs.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import httpx
import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.ads import pinterest
from apps.ads.models import AdsConnection
from apps.ads.services import AdsError
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import PlatformVisibility
from providers import pinterest_ads
from providers.exceptions import APIError, TokenExpiredError

ACCOUNT = "549755885175"


class FakePinterest:
    """Answers ``_request`` from a ``{(METHOD, path): body}`` table and records every call;
    a body that is an exception is raised."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, client, method, url, **kwargs):
        path = url.split("/v5/", 1)[1]
        self.calls.append((method, path, {**kwargs, "url": url}))
        body = self.routes.get((method, path))
        if body is None:
            raise AssertionError(f"unexpected Pinterest call {method} {path}")
        if isinstance(body, Exception):
            raise body
        return httpx.Response(200, json=body, request=httpx.Request(method, url))

    def sent(self, method: str, path: str) -> dict:
        return next(kwargs for m, p, kwargs in self.calls if m == method and p == path)


def fake_pinterest(routes):
    fake = FakePinterest(routes)
    return fake, mock.patch.object(pinterest_ads.PinterestAdsClient, "_request", autospec=True, side_effect=fake)


def _batch(data):
    return {"items": [{"data": data, "exceptions": []}]}


@pytest.fixture(autouse=True)
def _pinterest_app(settings):
    settings.BILLING_REQUIRE_SUBSCRIPTION = False
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {
        **settings.PLATFORM_CREDENTIALS_FROM_ENV,
        "pinterest": {"app_id": "pin-app", "app_secret": "pin-secret"},
    }


@pytest.fixture
def workspace(organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Pins WS", organization=organization)


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
    return _member(workspace, "pin-manager@example.com", WorkspaceMembership.WorkspaceRole.MANAGER)


@pytest.fixture
def viewer(workspace):
    return _member(workspace, "pin-viewer@example.com", WorkspaceMembership.WorkspaceRole.VIEWER)


@pytest.fixture
def connection(workspace, manager):
    return AdsConnection.objects.create(
        workspace=workspace,
        platform=AdsConnection.Platform.PINTEREST,
        access_token="pin-token",
        refresh_token="pin-refresh",
        token_expires_at=timezone.now() + timedelta(days=20),
        meta_user_id="user-1",
        ad_account_id=ACCOUNT,
        ad_account_name="Loja",
        currency="BRL",
        status=AdsConnection.Status.CONNECTED,
        connected_by=manager,
    )


def _logged_in(user):
    client = Client()
    client.force_login(user)
    return client


NO_CHOICES = {"interests": [], "audiences": [], "conversion_tags": [], "errors": {}}


@pytest.fixture
def no_choices():
    with mock.patch.object(pinterest, "targeting_choices", return_value=NO_CHOICES):
        yield


def _new_campaign(**overrides):
    values = dict(
        name="Natal",
        objective="CONSIDERATION",
        daily_budget=Decimal("25.50"),
        start_time=None,
        end_time=None,
        countries=["BR"],
        pin_id="1129559150354629778",
    )
    values.update(overrides)
    return pinterest.NewCampaign(**values)


# ------------------------------------------------------------------
# Client
# ------------------------------------------------------------------


class TestClient:
    def test_analytics_in_micro_currency_become_the_cards_numbers(self):
        row = pinterest_ads.parse_analytics_row(
            {
                "SPEND_IN_MICRO_DOLLAR": 50_000_000,
                "PAID_IMPRESSION": 2000,
                "TOTAL_IMPRESSION_USER": 1000,
                "CLICKTHROUGH_1": 40,
                "OUTBOUND_CLICK_1": 25,
                "TOTAL_CHECKOUT": 2,
                "TOTAL_CHECKOUT_VALUE_IN_MICRO_DOLLAR": 150_000_000,
                "CAMPAIGN_ID": "77",
            }
        )
        assert row.spend == Decimal(50)
        assert row.ctr == Decimal(2)  # 40 clicks / 2000 impressions, in %
        assert row.cpc == Decimal("1.25")
        assert row.cpm == Decimal(25)
        assert row.link_clicks == 25
        assert row.roas == Decimal(3)
        assert row.frequency == Decimal(2)
        assert row.campaign_id == "77"

    def test_a_missing_column_reads_as_nothing_spent(self):
        row = pinterest_ads.parse_analytics_row({"AD_ACCOUNT_ID": ACCOUNT})
        assert row.spend == 0
        assert row.cpc is None
        assert row.roas is None

    def test_a_401_asks_to_sign_in_again_keeping_pinterest_sentence(self):
        client = pinterest_ads.PinterestAdsClient({"app_id": "a", "app_secret": "s"})
        exc = client._error_for_response(httpx.Response(401, json={"code": 2, "message": "Authentication failed."}))
        assert isinstance(exc, TokenExpiredError)
        assert exc.user_message == "Authentication failed."

    def test_a_refused_batch_item_is_an_error_with_its_message(self):
        client = pinterest_ads.PinterestAdsClient({"app_id": "a", "app_secret": "s"})
        refused = {"items": [{"data": None, "exceptions": [{"code": 2683, "message": "Daily budget too low."}]}]}
        fake, patch = fake_pinterest({("POST", f"ad_accounts/{ACCOUNT}/campaigns"): refused})
        with patch, pytest.raises(APIError) as caught:
            client.create_campaign("token", ACCOUNT, {"name": "x"})
        assert caught.value.user_message == "Daily budget too low."

    def test_list_filters_go_as_one_comma_separated_value(self):
        """Pinterest reads only the last of a repeated parameter, which hid paused campaigns."""
        fake, patch = fake_pinterest({("GET", f"ad_accounts/{ACCOUNT}/campaigns"): {"items": [], "bookmark": None}})
        with patch:
            pinterest_ads.PinterestAdsClient({"app_id": "a", "app_secret": "s"}).campaigns("token", ACCOUNT)
        params = fake.sent("GET", f"ad_accounts/{ACCOUNT}/campaigns")["params"]
        assert params["entity_statuses"] == "ACTIVE,PAUSED,ARCHIVED"

    def test_connect_asks_for_the_ads_scopes(self):
        client = pinterest_ads.PinterestAdsClient({"app_id": "a", "app_secret": "s"})
        url = client.get_auth_url("https://app.example.com/ads/pinterest/callback/", "state")
        assert "ads%3Aread%2Cads%3Awrite" in url


# ------------------------------------------------------------------
# The ad-account boundary and the token
# ------------------------------------------------------------------


@pytest.mark.django_db
class TestBoundary:
    def test_a_campaign_is_only_read_inside_the_chosen_ad_account(self, connection):
        fake, patch = fake_pinterest(
            {("GET", f"ad_accounts/{ACCOUNT}/campaigns/55"): {"id": "55", "ad_account_id": ACCOUNT, "name": "x"}}
        )
        with patch:
            campaign = pinterest.get_campaign(connection, "55")
        assert campaign.id == "55"
        assert [path for _m, path, _k in fake.calls] == [f"ad_accounts/{ACCOUNT}/campaigns/55"]

    def test_a_non_numeric_campaign_id_is_refused_without_calling_pinterest(self, connection):
        fake, patch = fake_pinterest({})
        with patch, pytest.raises(AdsError):
            pinterest.get_campaign(connection, "55/../../ad_accounts")
        assert fake.calls == []

    def test_choosing_an_account_the_token_does_not_reach_is_refused(self, connection):
        fake, patch = fake_pinterest(
            {("GET", "ad_accounts"): {"items": [{"id": ACCOUNT, "name": "Loja", "currency": "BRL"}], "bookmark": None}}
        )
        with patch, pytest.raises(AdsError):
            pinterest.choose_account(connection, "999")
        connection.refresh_from_db()
        assert connection.ad_account_id == ACCOUNT

    def test_a_401_renews_the_token_once_and_retries(self, connection):
        expired = TokenExpiredError("expired", status_code=401)
        campaign = {"id": "55", "ad_account_id": ACCOUNT, "name": "x"}
        fake, patch = fake_pinterest(
            {
                ("POST", "oauth/token"): {
                    "access_token": "new-token",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            }
        )
        answers = iter([expired, campaign])

        def campaign_call(client, token, ad_account_id, campaign_id):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            assert token == "new-token"
            return answer

        campaign_call.__name__ = "campaign"
        with patch, mock.patch.object(pinterest_ads.PinterestAdsClient, "campaign", campaign_call):
            pinterest._call(connection, pinterest_ads.PinterestAdsClient.campaign, ACCOUNT, "55")
        connection.refresh_from_db()
        assert connection.access_token == "new-token"
        assert connection.refresh_token == "new-refresh"
        assert connection.status == AdsConnection.Status.CONNECTED

    def test_a_refused_renewal_marks_the_connection(self, connection):
        fake, patch = fake_pinterest(
            {
                ("GET", f"ad_accounts/{ACCOUNT}/campaigns/55"): TokenExpiredError("expired", status_code=401),
                ("POST", "oauth/token"): TokenExpiredError("refresh refused", status_code=401),
            }
        )
        with patch, pytest.raises(AdsError):
            pinterest.get_campaign(connection, "55")
        connection.refresh_from_db()
        assert connection.status == AdsConnection.Status.ERROR

    def test_the_meta_connection_is_not_the_pinterest_one(self, workspace, connection):
        from apps.ads import services

        assert services.get_connection(workspace.id) is None
        AdsConnection.objects.create(workspace=workspace, access_token="meta", ad_account_id="act_1")
        assert services.get_connection(workspace.id).platform == AdsConnection.Platform.META
        assert pinterest.get_connection(workspace.id) == connection


# ------------------------------------------------------------------
# Periods: Pinterest keeps 90 days
# ------------------------------------------------------------------


@pytest.mark.django_db
class TestWindows:
    def test_a_period_older_than_90_days_is_refused(self, connection):
        with pytest.raises(AdsError):
            pinterest._windows(30, timezone.localdate() - timedelta(days=80))

    def test_without_the_previous_window_nothing_is_compared(self, connection):
        campaigns = {"items": [{"id": "77", "name": "Natal", "status": "ACTIVE", "summary_status": "RUNNING"}]}
        numbers = [{"CAMPAIGN_ID": "77", "SPEND_IN_MICRO_DOLLAR": 10_000_000, "CLICKTHROUGH_1": 5}]
        fake, patch = fake_pinterest(
            {
                ("GET", f"ad_accounts/{ACCOUNT}/campaigns"): campaigns,
                ("GET", f"ad_accounts/{ACCOUNT}/campaigns/analytics"): numbers,
            }
        )
        with patch:
            (campaign,) = pinterest.list_campaigns(connection, days=90)
        assert campaign.insights.spend == Decimal(10)
        assert campaign.compared is False
        assert campaign.key_change is None
        analytics = [k for m, p, k in fake.calls if p.endswith("/campaigns/analytics")]
        assert len(analytics) == 1


# ------------------------------------------------------------------
# Creation
# ------------------------------------------------------------------


CREATION_ROUTES = {
    ("POST", f"ad_accounts/{ACCOUNT}/campaigns"): _batch({"id": "c-1"}),
    ("POST", f"ad_accounts/{ACCOUNT}/ad_groups"): _batch({"id": "ag-1"}),
    ("POST", f"ad_accounts/{ACCOUNT}/ads"): _batch({"id": "ad-1"}),
}


@pytest.mark.django_db
class TestCreation:
    def test_the_campaign_is_paused_and_promotes_the_pin(self, connection):
        fake, patch = fake_pinterest(dict(CREATION_ROUTES))
        with patch:
            campaign_id = pinterest.create_campaign(connection, _new_campaign(countries=["BR", "PT"]))
        assert campaign_id == "c-1"
        (campaign,) = fake.sent("POST", f"ad_accounts/{ACCOUNT}/campaigns")["json"]
        assert campaign["status"] == "PAUSED"
        assert campaign["objective_type"] == "CONSIDERATION"
        assert campaign["daily_spend_cap"] == 25_500_000
        (group,) = fake.sent("POST", f"ad_accounts/{ACCOUNT}/ad_groups")["json"]
        assert group["campaign_id"] == "c-1"
        assert group["status"] == "ACTIVE"
        assert group["billable_event"] == "CLICKTHROUGH"
        assert group["bid_strategy_type"] == "AUTOMATIC_BID"
        assert group["targeting_spec"] == {"GEO": ["BR", "PT"]}
        assert "budget_in_micro_currency" not in group
        (ad,) = fake.sent("POST", f"ad_accounts/{ACCOUNT}/ads")["json"]
        assert ad == {
            "ad_group_id": "ag-1",
            "pin_id": "1129559150354629778",
            "creative_type": "REGULAR",
            "status": "ACTIVE",
            "name": "Natal",
        }

    def test_a_refused_ad_archives_the_campaign_again(self, connection):
        refused = {"items": [{"data": None, "exceptions": [{"message": "Pin not promotable."}]}]}
        routes = {
            **CREATION_ROUTES,
            ("POST", f"ad_accounts/{ACCOUNT}/ads"): refused,
            ("PATCH", f"ad_accounts/{ACCOUNT}/campaigns"): _batch({"id": "c-1", "status": "ARCHIVED"}),
        }
        fake, patch = fake_pinterest(routes)
        with patch, pytest.raises(AdsError) as caught:
            pinterest.create_campaign(connection, _new_campaign())
        assert "Pin not promotable." in str(caught.value)
        assert fake.sent("PATCH", f"ad_accounts/{ACCOUNT}/campaigns")["json"] == [{"id": "c-1", "status": "ARCHIVED"}]

    def test_an_objective_not_offered_is_refused_before_spending_anything(self, connection):
        fake, patch = fake_pinterest({})
        with patch, pytest.raises(AdsError):
            pinterest.create_campaign(connection, _new_campaign(objective="LEADS"))
        assert fake.calls == []

    def test_only_this_workspaces_published_pins_are_offered(self, workspace, connection, organization, manager):
        from apps.composer.models import PlatformPost, Post
        from apps.social_accounts.models import SocialAccount
        from apps.workspaces.models import Workspace

        def pin(ws, pin_id, status=PlatformPost.Status.PUBLISHED):
            account = SocialAccount.objects.create(
                workspace=ws, platform="pinterest", account_platform_id=f"acc-{pin_id}", account_name="Pins"
            )
            post = Post.objects.create(workspace=ws, author=manager, caption=f"pin {pin_id}")
            PlatformPost.objects.create(post=post, social_account=account, status=status, platform_post_id=pin_id)

        pin(workspace, "111")
        pin(workspace, "222", status=PlatformPost.Status.FAILED)
        pin(Workspace.objects.create(name="Other", organization=organization), "333")
        assert [p["id"] for p in pinterest.promotable_pins(workspace)] == ["111"]


# ------------------------------------------------------------------
# Who may do what
# ------------------------------------------------------------------


def _url(name, workspace, **kwargs):
    return reverse(f"ads:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


@pytest.mark.django_db
class TestPermissions:
    def test_a_viewer_can_read_but_not_change(self, workspace, viewer, connection):
        client = _logged_in(viewer)
        assert client.get(_url("pinterest_index", workspace)).status_code == 200
        fake, patch = fake_pinterest({})
        with patch:
            response = client.post(_url("pinterest_campaign_status", workspace, campaign_id="55"), {"status": "ACTIVE"})
            assert client.get(_url("pinterest_campaign_new", workspace)).status_code == 403
            assert client.post(_url("pinterest_disconnect", workspace)).status_code == 403
        assert response.status_code == 403
        assert fake.calls == []
        assert AdsConnection.objects.filter(pk=connection.pk).exists()

    def test_another_workspace_member_never_reaches_this_connection(self, connection, organization):
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(name="Other", organization=organization)
        outsider = _member(other, "outsider@example.com", WorkspaceMembership.WorkspaceRole.MANAGER)
        client = _logged_in(outsider)
        fake, patch = fake_pinterest({})
        with patch:
            for name, kwargs in (
                ("pinterest_overview", {}),
                ("pinterest_campaign_body", {"campaign_id": "55"}),
                ("pinterest_settings", {}),
            ):
                assert client.get(_url(name, connection.workspace, **kwargs)).status_code in (403, 404), name
            assert client.post(_url("pinterest_disconnect", connection.workspace)).status_code in (403, 404)
        assert fake.calls == []
        assert AdsConnection.objects.filter(pk=connection.pk).exists()

    def test_a_meta_sign_in_state_is_not_accepted_here(self, workspace, manager):
        from apps.social_accounts.views import _sign_state

        client = _logged_in(manager)
        session = client.session
        session["pinterest_ads_oauth"] = {"nonce": "n", "workspace_id": str(workspace.id)}
        session.save()
        state = _sign_state(workspace.id, "meta_ads", manager.id, "n")
        fake, patch = fake_pinterest({})
        with patch:
            client.get(reverse("ads_callback:pinterest_callback"), {"code": "c", "state": state})
        assert fake.calls == []
        assert not AdsConnection.objects.filter(workspace=workspace).exists()

    def test_the_ads_entry_leads_to_pinterest_when_it_is_the_only_one(self, workspace, viewer, connection):
        response = _logged_in(viewer).get(_url("index", workspace))
        assert response.status_code == 302
        assert response["Location"] == _url("pinterest_index", workspace)


# ------------------------------------------------------------------
# Social Accounts page
# ------------------------------------------------------------------


@pytest.mark.django_db
class TestSocialAccountsCard:
    def test_pinterest_ads_is_offered_to_connect(self, workspace, manager):
        response = _logged_in(manager).get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id}))
        body = response.content.decode()
        assert "guide = 'pinterest_ads'" in body
        assert _url("pinterest_connect", workspace) in body

    def test_a_connected_ad_account_can_be_disconnected_from_its_card(self, workspace, manager, connection):
        client = _logged_in(manager)
        body = client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})).content.decode()
        assert _url("pinterest_disconnect", workspace) in body
        assert _url("pinterest_index", workspace) in body
        response = client.post(_url("pinterest_disconnect", workspace), {"return_to": "accounts"})
        assert response["Location"] == reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})
        assert not AdsConnection.objects.filter(pk=connection.pk).exists()

    def test_hiding_pinterest_hides_its_ads_card_and_refuses_the_sign_in(self, workspace, manager):
        PlatformVisibility.set_visible("pinterest", False)
        client = _logged_in(manager)
        body = client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})).content.decode()
        assert "guide = 'pinterest_ads'" not in body
        assert _url("pinterest_connect", workspace) not in body

        response = client.post(_url("pinterest_connect", workspace))
        assert response["Location"] == reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})
        assert "pinterest.com/oauth" not in response["Location"]

    def test_a_workspace_already_connected_keeps_its_card_when_pinterest_is_hidden(
        self, workspace, manager, connection
    ):
        PlatformVisibility.set_visible("pinterest", False)
        body = (
            _logged_in(manager)
            .get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id}))
            .content.decode()
        )
        assert _url("pinterest_disconnect", workspace) in body


# ------------------------------------------------------------------
# Screens
# ------------------------------------------------------------------

CAMPAIGN_ROW = {
    "id": "77",
    "ad_account_id": ACCOUNT,
    "name": "Natal",
    "status": "ACTIVE",
    "summary_status": "RUNNING",
    "objective_type": "CONSIDERATION",
    "daily_spend_cap": 30_000_000,
}
NUMBERS = [{"CAMPAIGN_ID": "77", "SPEND_IN_MICRO_DOLLAR": 12_500_000, "PAID_IMPRESSION": 900, "CLICKTHROUGH_1": 9}]


@pytest.mark.django_db
class TestScreens:
    def test_the_overview_brings_the_numbers_and_campaigns(self, workspace, viewer, connection):
        fake, patch = fake_pinterest(
            {
                ("GET", f"ad_accounts/{ACCOUNT}/analytics"): [{"AD_ACCOUNT_ID": ACCOUNT, **NUMBERS[0]}],
                ("GET", f"ad_accounts/{ACCOUNT}/campaigns"): {"items": [CAMPAIGN_ROW], "bookmark": None},
                ("GET", f"ad_accounts/{ACCOUNT}/campaigns/analytics"): NUMBERS,
            }
        )
        with patch:
            response = _logged_in(viewer).get(_url("pinterest_overview", workspace), {"range": "7"})
        body = response.content.decode()
        assert response.status_code == 200
        assert "Natal" in body
        assert _url("pinterest_campaign_detail", workspace, campaign_id="77") in body
        assert f"https://ads.pinterest.com/advertiser/{ACCOUNT}/" in body

    def test_the_campaign_page_and_its_body(self, workspace, manager, connection):
        routes = {
            ("GET", f"ad_accounts/{ACCOUNT}/campaigns/77"): CAMPAIGN_ROW,
            ("GET", f"ad_accounts/{ACCOUNT}/campaigns/analytics"): NUMBERS,
            ("GET", f"ad_accounts/{ACCOUNT}/ad_groups"): {
                "items": [
                    {"id": "ag-1", "name": "Grupo", "summary_status": "RUNNING", "targeting_spec": {"GEO": ["BR"]}}
                ]
            },
            ("GET", f"ad_accounts/{ACCOUNT}/ads"): {
                "items": [{"id": "ad-1", "pin_id": "123", "summary_status": "RUNNING", "review_status": "PENDING"}]
            },
        }
        client = _logged_in(manager)
        fake, patch = fake_pinterest(routes)
        with patch:
            page = client.get(_url("pinterest_campaign_detail", workspace, campaign_id="77")).content.decode()
            body = client.get(_url("pinterest_campaign_body", workspace, campaign_id="77"), {"range": "7"})
        assert _url("pinterest_campaign_edit", workspace, campaign_id="77") in page
        assert body.status_code == 200
        text = body.content.decode()
        assert "Grupo" in text
        assert "https://www.pinterest.com/pin/123/" in text

    def test_the_new_campaign_page_lists_the_published_pins(self, workspace, manager, connection, no_choices):
        with mock.patch.object(
            pinterest,
            "promotable_pins",
            return_value=[{"id": "123", "title": "Meu pin", "caption": "", "account": "Pins"}],
        ):
            response = _logged_in(manager).get(_url("pinterest_campaign_new", workspace))
        assert response.status_code == 200
        assert "Meu pin" in response.content.decode()

    def test_each_published_pin_is_offered_with_its_image(self, workspace, manager, connection, no_choices):
        from apps.composer.models import PlatformPost, Post, PostMedia
        from apps.media_library.models import MediaAsset
        from apps.social_accounts.models import SocialAccount

        account = SocialAccount.objects.create(
            workspace=workspace, platform="pinterest", account_platform_id="acc", account_name="Pins"
        )
        post = Post.objects.create(workspace=workspace, author=manager, caption="Novidade")
        asset = MediaAsset.objects.create(
            organization=workspace.organization,
            workspace=workspace,
            uploaded_by=manager,
            file="test/robo-pinterest.jpg",
            filename="robo-pinterest.jpg",
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/jpeg",
        )
        PostMedia.objects.create(post=post, media_asset=asset)
        PlatformPost.objects.create(
            post=post, social_account=account, status=PlatformPost.Status.PUBLISHED, platform_post_id="444"
        )

        body = _logged_in(manager).get(_url("pinterest_campaign_new", workspace)).content.decode()
        assert 'value="444"' in body
        assert "robo-pinterest.jpg" in body
        assert 'data-image="/media/test/robo-pinterest.jpg"' in body
        assert 'data-account="Pins"' in body

    def test_the_settings_list_the_ad_accounts_and_offer_a_new_one(self, workspace, manager, connection):
        fake, patch = fake_pinterest(
            {("GET", "ad_accounts"): {"items": [{"id": ACCOUNT, "name": "Loja", "currency": "BRL"}], "bookmark": None}}
        )
        with patch:
            response = _logged_in(manager).get(
                _url("pinterest_settings", workspace), {"partial": "body"}, HTTP_HX_REQUEST="true"
            )
        body = response.content.decode()
        assert "Loja" in body
        assert _url("pinterest_account_create", workspace) in body


# ------------------------------------------------------------------
# The full creation: audience, placement, bid
# ------------------------------------------------------------------


@pytest.mark.django_db
class TestFullCreation:
    def test_the_ad_group_carries_the_whole_audience_placement_and_bid(self, connection):
        fake, patch = fake_pinterest(
            {**CREATION_ROUTES, ("POST", f"ad_accounts/{ACCOUNT}/keywords"): {"keywords": [{}], "errors": []}}
        )
        new = _new_campaign(
            regions=["BR-SP", "BR-RJ"],
            age_min="25",
            age_max="44",
            genders=["female"],
            locales=["pt"],
            interests=["935249274030"],
            keywords=["moveis planejados", "marcenaria"],
            audiences_include=["2542620905473"],
            audiences_exclude=["2542620905475"],
            auto_targeting=False,
            placement="SEARCH",
            devices=["iphone", "android_mobile"],
            bid_strategy="MAX_BID",
            bid_amount=Decimal("1.50"),
        )
        with patch:
            pinterest.create_campaign(connection, new)
        (group,) = fake.sent("POST", f"ad_accounts/{ACCOUNT}/ad_groups")["json"]
        assert group["targeting_spec"] == {
            "GEO": ["BR-SP", "BR-RJ"],
            "MINIMUM_AGE": "25",
            "MAXIMUM_AGE": "44",
            "GENDER": ["female"],
            "LOCALE": ["pt"],
            "INTEREST": ["935249274030"],
            "AUDIENCE_INCLUDE": ["2542620905473"],
            "AUDIENCE_EXCLUDE": ["2542620905475"],
            "APPTYPE": ["iphone", "android_mobile"],
        }
        assert group["placement_group"] == "SEARCH"
        assert group["auto_targeting_enabled"] is False
        assert group["bid_strategy_type"] == "MAX_BID"
        assert group["bid_in_micro_currency"] == 1_500_000
        keywords = fake.sent("POST", f"ad_accounts/{ACCOUNT}/keywords")["json"]
        assert keywords == {
            "parent_id": "ag-1",
            "keywords": [
                {"match_type": "BROAD", "value": "moveis planejados"},
                {"match_type": "BROAD", "value": "marcenaria"},
            ],
        }

    def test_a_sales_campaign_budgets_its_ad_group_and_optimises_for_the_tag(self, connection):
        fake, patch = fake_pinterest(dict(CREATION_ROUTES))
        with patch:
            pinterest.create_campaign(connection, _new_campaign(objective="SALES", conversion_tag_id="2612345"))
        (campaign,) = fake.sent("POST", f"ad_accounts/{ACCOUNT}/campaigns")["json"]
        assert "daily_spend_cap" not in campaign
        (group,) = fake.sent("POST", f"ad_accounts/{ACCOUNT}/ad_groups")["json"]
        assert group["budget_in_micro_currency"] == 25_500_000
        assert group["billable_event"] == "IMPRESSION"
        goal = group["optimization_goal_metadata"]["conversion_tag_v3_goal_metadata"]
        assert goal["conversion_tag_id"] == "2612345"
        assert goal["conversion_event"] == "CHECKOUT"

    def test_refused_keywords_archive_the_campaign_again(self, connection):
        routes = {
            **CREATION_ROUTES,
            ("POST", f"ad_accounts/{ACCOUNT}/keywords"): {"keywords": [], "errors": [{"message": "Bad keyword."}]},
            ("PATCH", f"ad_accounts/{ACCOUNT}/campaigns"): _batch({"id": "c-1", "status": "ARCHIVED"}),
        }
        fake, patch = fake_pinterest(routes)
        with patch, pytest.raises(AdsError) as caught:
            pinterest.create_campaign(connection, _new_campaign(keywords=["x"]))
        assert "Bad keyword." in str(caught.value)
        assert fake.sent("PATCH", f"ad_accounts/{ACCOUNT}/campaigns")["json"] == [{"id": "c-1", "status": "ARCHIVED"}]
        assert not [c for c in fake.calls if c[1] == f"ad_accounts/{ACCOUNT}/ads"]

    def test_the_choices_come_from_the_ad_account_and_a_refusal_does_not_break_the_form(self, connection):
        from django.core.cache import cache

        cache.delete(f"pinterest-ads:interests:{ACCOUNT}")
        interests = [
            {"id": "1", "name": "Home decor", "child_interests": [{"id": "2", "name": "Bedroom"}]},
            {"3": "Art"},
        ]
        routes = {
            ("GET", "resources/targeting/interest"): interests,
            ("GET", f"ad_accounts/{ACCOUNT}/audiences"): {
                "items": [{"id": "9", "name": "Clientes", "status": "READY"}, {"id": "8", "status": "TOO_SMALL"}],
                "bookmark": None,
            },
            ("GET", f"ad_accounts/{ACCOUNT}/conversion_tags"): APIError(
                "403", status_code=403, platform="Pinterest Ads", user_message="No access to tags."
            ),
        }
        fake, patch = fake_pinterest(routes)
        with patch:
            choices = pinterest.targeting_choices(connection)
        assert [o["name"] for o in choices["interests"]] == ["Art", "Bedroom", "Home decor"]
        assert choices["audiences"] == [{"id": "9", "name": "Clientes"}]
        assert choices["conversion_tags"] == []
        assert "No access to tags." in choices["errors"]["conversion_tags"]


FORM_POST = {
    "name": "Natal",
    "objective": "CONSIDERATION",
    "pin_id": "123",
    "countries": ["BR"],
    "placement": "ALL",
    "bid_strategy": "AUTOMATIC_BID",
    "daily_budget": "30",
    "auto_targeting": "on",
}
PINS = [{"id": "123", "title": "Meu pin", "caption": "", "account": "Pins", "is_video": False}]
CHOICES = {
    "interests": [{"id": "77", "name": "Home decor"}],
    "audiences": [{"id": "9", "name": "Clientes"}],
    "conversion_tags": [],
    "errors": {},
}


def _form(connection, **changes):
    from apps.ads.forms import PinterestCampaignCreateForm

    data = {**FORM_POST, **changes}
    return PinterestCampaignCreateForm(data, connection=connection, pins=PINS, choices=CHOICES)


@pytest.mark.django_db
class TestCreationForm:
    def test_a_full_form_becomes_the_new_campaign(self, connection):
        form = _form(
            connection,
            age_min="25",
            age_max="65+",
            genders=["male"],
            interests=["77"],
            keywords="marcenaria, Marcenaria\nmóveis",
            devices=["web"],
        )
        assert form.is_valid(), form.errors
        new = form.to_new_campaign()
        assert new.keywords == ["marcenaria", "móveis"]
        assert new.interests == ["77"]
        assert new.age_max == "65+"
        assert new.auto_targeting is True

    @pytest.mark.parametrize(
        ("changes", "field", "step"),
        [
            ({"keywords": "x", "placement": "BROWSE"}, "keywords", 2),
            ({"objective": "SALES"}, "conversion_tag_id", 1),
            ({"objective": "VIDEO_COMPLETION"}, "objective", 1),
            ({"countries": ["BR", "PT"], "regions": ["BR-SP"]}, "regions", 2),
            ({"audiences_include": ["9"], "audiences_exclude": ["9"]}, "audiences_exclude", 2),
            ({"bid_strategy": "MAX_BID"}, "bid_amount", 4),
            ({"age_min": "40", "age_max": "30"}, "age_max", 2),
            ({"frequency_cap": "3"}, "frequency_cap", 4),
            ({"interests": ["not-offered"]}, "interests", 2),
        ],
    )
    def test_what_pinterest_would_refuse_is_caught_on_its_step(self, connection, changes, field, step):
        form = _form(connection, **changes)
        assert not form.is_valid()
        assert field in form.errors
        assert form.first_step_with_errors() == step

    def test_a_refused_post_reopens_the_step_of_the_error(self, workspace, manager, connection):
        with (
            mock.patch.object(pinterest, "targeting_choices", return_value=CHOICES),
            mock.patch.object(pinterest, "promotable_pins", return_value=PINS),
        ):
            response = _logged_in(manager).post(
                _url("pinterest_campaign_new", workspace), {**FORM_POST, "keywords": "x", "placement": "BROWSE"}
            )
        assert response.status_code == 200
        assert "campaignWizard(2)" in response.content.decode()

    def test_a_valid_post_creates_the_campaign_with_its_audience(self, workspace, manager, connection):
        fake, patch = fake_pinterest(dict(CREATION_ROUTES))
        with (
            patch,
            mock.patch.object(pinterest, "targeting_choices", return_value=CHOICES),
            mock.patch.object(pinterest, "promotable_pins", return_value=PINS),
        ):
            response = _logged_in(manager).post(
                _url("pinterest_campaign_new", workspace), {**FORM_POST, "interests": ["77"], "genders": ["female"]}
            )
        assert response.status_code == 302
        (group,) = fake.sent("POST", f"ad_accounts/{ACCOUNT}/ad_groups")["json"]
        assert group["targeting_spec"]["INTEREST"] == ["77"]
        assert group["targeting_spec"]["GENDER"] == ["female"]
