"""Radar: connecting Shopee Affiliates, who may do what, tenant isolation and the offers list."""

from __future__ import annotations

import io
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import connection as db_connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User
from apps.composer.models import Post, PostMedia
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.radar import services
from apps.radar.models import OpportunityConnection, SavedOffer
from apps.tasks.models import Task
from apps.workspaces.models import Workspace
from providers.exceptions import OAuthError
from providers.shopee_affiliate import Offer, OfferPage, offer_to_dict

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_english", "_clear_cache")]

Role = WorkspaceMembership.WorkspaceRole


@pytest.fixture
def _english(settings):
    settings.LANGUAGE_CODE = "en-us"


@pytest.fixture
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _workspace(name):
    org = Organization.objects.create(name=f"{name} org")
    return Workspace.objects.create(organization=org, name=name)


def _user(email, workspace, role):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.get_or_create(
        user=user, organization=workspace.organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
    )
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def _offer(item_id, *, price="50", commission="5", rate="0.10", discount=0, rating="4.8", sales=10):
    return Offer(
        item_id=item_id,
        name=f"Product {item_id}",
        image_url="https://cf.shopee.com.br/file/x",
        shop_name="Shop",
        price=Decimal(price),
        price_min=Decimal(price),
        price_max=Decimal(price),
        discount_rate=discount,
        rating=Decimal(rating) if rating else None,
        sales=sales,
        commission_rate=Decimal(rate),
        commission=Decimal(commission),
        seller_commission_rate=Decimal(rate),
        shopee_commission_rate=Decimal(0),
        product_link=f"https://shopee.com.br/product/1/{item_id}",
        offer_link=f"https://s.shopee.com.br/{item_id}",
        category_ids=(100631,),
    )


@pytest.fixture
def workspace():
    return _workspace("Acme")


@pytest.fixture
def owner(workspace):
    return _user("owner@example.com", workspace, Role.OWNER)


@pytest.fixture
def shopee(workspace, owner):
    return OpportunityConnection.objects.create(
        workspace=workspace,
        source=OpportunityConnection.Source.SHOPEE_AFFILIATE,
        account_label="18340070022",
        credentials={"app_id": "18340070022", "secret": "top-secret-value"},
        connected_by=owner,
    )


def _url(name, workspace, **kwargs):
    return reverse(f"radar:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


# ----------------------------------------------------------------------
# Connecting
# ----------------------------------------------------------------------


def test_connect_checks_with_shopee_then_stores_the_secret_encrypted(workspace, owner):
    with patch("apps.radar.services.ShopeeAffiliateClient.verify") as verify:
        response = _client(owner).post(
            _url("connect_shopee", workspace), {"app_id": "18340070022", "secret": "top-secret-value"}
        )

    assert response.status_code == 302
    assert response.url == _url("shopee", workspace)
    verify.assert_called_once()
    conn = OpportunityConnection.objects.get(workspace=workspace)
    assert conn.credentials == {"app_id": "18340070022", "secret": "top-secret-value"}
    with db_connection.cursor() as cursor:
        cursor.execute("SELECT credentials FROM radar_opportunityconnection WHERE id = %s", [conn.id])
        assert "top-secret-value" not in cursor.fetchone()[0]


def test_refused_credentials_save_nothing_and_say_so_in_the_modal(workspace, owner):
    with patch("apps.radar.services.ShopeeAffiliateClient.verify", side_effect=OAuthError("bad")):
        response = _client(owner).post(
            _url("connect_shopee", workspace),
            {"app_id": "18340070022", "secret": "wrong"},
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    assert b"refused this App ID and Secret" in response.content
    assert b"<html" not in response.content
    assert not OpportunityConnection.objects.exists()


def test_successful_connect_from_the_modal_redirects_with_htmx(workspace, owner):
    with patch("apps.radar.services.ShopeeAffiliateClient.verify"):
        response = _client(owner).post(
            _url("connect_shopee", workspace),
            {"app_id": "18340070022", "secret": "top-secret-value"},
            HTTP_HX_REQUEST="true",
        )

    assert response.status_code == 200
    assert response["HX-Redirect"] == _url("shopee", workspace)


def test_reconnecting_replaces_the_single_connection(workspace, owner, shopee):
    with patch("apps.radar.services.ShopeeAffiliateClient.verify"):
        _client(owner).post(_url("connect_shopee", workspace), {"app_id": "999999", "secret": "new"})

    conn = OpportunityConnection.objects.get(workspace=workspace)
    assert conn.pk == shopee.pk
    assert conn.credentials == {"app_id": "999999", "secret": "new"}


def test_app_id_must_be_numeric(workspace, owner):
    with patch("apps.radar.services.ShopeeAffiliateClient.verify") as verify:
        response = _client(owner).post(_url("connect_shopee", workspace), {"app_id": "abc", "secret": "s"})

    assert response.status_code == 200
    verify.assert_not_called()


# ----------------------------------------------------------------------
# Permissions and isolation
# ----------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.EDITOR, Role.CONTRIBUTOR])
def test_only_account_managers_connect_or_disconnect(workspace, shopee, role):
    member = _client(_user("member@example.com", workspace, role))

    assert member.get(_url("connect_shopee", workspace)).status_code == 403
    assert member.post(_url("disconnect", workspace, connection_id=shopee.id)).status_code == 403
    assert OpportunityConnection.objects.filter(pk=shopee.pk).exists()


@pytest.mark.parametrize("role", [Role.CLIENT, Role.VIEWER])
def test_clients_and_viewers_cannot_open_the_radar(workspace, shopee, role):
    guest = _client(_user("guest@example.com", workspace, role))

    assert guest.get(_url("index", workspace)).status_code == 403
    assert guest.get(_url("shopee", workspace)).status_code == 403


def test_cannot_disconnect_another_workspaces_connection(workspace, shopee):
    other = _workspace("Other")
    intruder = _client(_user("intruder@example.com", other, Role.OWNER))

    response = intruder.post(_url("disconnect", other, connection_id=shopee.id))

    assert response.status_code == 404
    assert OpportunityConnection.objects.filter(pk=shopee.pk).exists()


def test_offers_never_use_another_workspaces_connection(workspace, shopee):
    other = _workspace("Other")
    intruder = _client(_user("intruder@example.com", other, Role.OWNER))

    with patch("apps.radar.services.ShopeeAffiliateClient.search_offers") as search:
        response = intruder.get(_url("shopee", other))

    assert response.status_code == 302
    assert response.url == _url("index", other)
    search.assert_not_called()


def test_secret_is_never_rendered(workspace, owner, shopee):
    page = OfferPage(offers=[_offer(1)], page=1, has_next_page=False)
    client = _client(owner)
    with patch("apps.radar.services.ShopeeAffiliateClient.search_offers", return_value=page):
        pages = [
            client.get(_url("shopee", workspace)),
            client.get(_url("index", workspace)),
            client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})),
            client.get(reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id})),
        ]

    for response in pages:
        assert response.status_code == 200
        assert b"top-secret-value" not in response.content


def test_disconnect_removes_the_connection(workspace, owner, shopee):
    response = _client(owner).post(_url("disconnect", workspace, connection_id=shopee.id))

    assert response.status_code == 302
    assert not OpportunityConnection.objects.exists()


@pytest.mark.parametrize(
    ("return_to", "page"),
    [("connect", "social_accounts:connect"), ("radar", "radar:index"), ("", "social_accounts:list")],
)
def test_disconnect_lands_where_it_was_clicked(workspace, owner, shopee, return_to, page):
    response = _client(owner).post(_url("disconnect", workspace, connection_id=shopee.id), {"return_to": return_to})

    assert response.url == reverse(page, kwargs={"workspace_id": workspace.id})


def test_connected_source_shows_one_account_row_with_its_disconnect(workspace, owner, shopee):
    response = _client(owner).get(reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id}))

    body = response.content.decode()
    assert "App ID 18340070022" in body
    assert _url("disconnect", workspace, connection_id=shopee.id) in body
    assert "Connect another account" not in body


# ----------------------------------------------------------------------
# Offers
# ----------------------------------------------------------------------


def test_shortcut_filters_and_sets_its_own_order(shopee):
    offers = [_offer(1, price="80"), _offer(2, price="15"), _offer(3, price="40"), _offer(4, price="0")]
    page = OfferPage(offers=offers, page=1, has_next_page=False)
    with patch("apps.radar.services.ShopeeAffiliateClient.search_offers", return_value=page):
        results = services.list_offers(shopee, services.OfferFilters(quick="under_50", sort="commission"))

    assert [o.item_id for o in results.offers] == [2, 3]


def test_advanced_filters_combine(shopee):
    offers = [
        _offer(1, rate="0.20", rating="4.9", sales=50),
        _offer(2, rate="0.05", rating="4.9", sales=50),
        _offer(3, rate="0.20", rating=None, sales=50),
        _offer(4, rate="0.20", rating="4.9", sales=1),
    ]
    page = OfferPage(offers=offers, page=1, has_next_page=False)
    filters = services.OfferFilters(min_commission_pct=Decimal(10), min_rating=Decimal("4.5"), min_sales=10)
    with patch("apps.radar.services.ShopeeAffiliateClient.search_offers", return_value=page):
        results = services.list_offers(shopee, filters)

    assert [o.item_id for o in results.offers] == [1]


def test_load_more_reuses_cached_pages_and_drops_duplicates(shopee):
    pages = {
        1: OfferPage(offers=[_offer(1, commission="9"), _offer(2, commission="1")], page=1, has_next_page=True),
        2: OfferPage(offers=[_offer(2, commission="1"), _offer(3, commission="5")], page=2, has_next_page=False),
    }
    with patch(
        "apps.radar.services.ShopeeAffiliateClient.search_offers", side_effect=lambda **kw: pages[kw["page"]]
    ) as search:
        services.list_offers(shopee, services.OfferFilters(pages=1))
        results = services.list_offers(shopee, services.OfferFilters(pages=2))

    assert search.call_count == 2
    assert [o.item_id for o in results.offers] == [1, 3, 2]
    assert results.has_more is False


def test_refused_credentials_flag_the_connection_and_show_why(workspace, owner, shopee):
    with patch("apps.radar.services.ShopeeAffiliateClient.search_offers", side_effect=OAuthError("10020")):
        response = _client(owner).get(_url("shopee", workspace))

    assert response.status_code == 200
    assert b"Reconnect Shopee Affiliates" in response.content
    shopee.refresh_from_db()
    assert shopee.status == OpportunityConnection.Status.ERROR


def test_htmx_filter_request_returns_only_the_results(workspace, owner, shopee):
    page = OfferPage(offers=[_offer(1)], page=1, has_next_page=True)
    with patch("apps.radar.services.ShopeeAffiliateClient.search_offers", return_value=page):
        response = _client(owner).get(_url("shopee", workspace), {"category": "100631"}, HTTP_HX_REQUEST="true")

    body = response.content.decode()
    assert response.status_code == 200
    assert body.lstrip().startswith('<div id="radar-offers">')
    assert "radar-share-data" in body
    assert "pages=2" in body


def test_share_texts_carry_the_affiliate_link():
    texts = services.share_texts(_offer(7, price="1234.5"))

    assert set(texts) == {"direct", "urgent", "simple"}
    assert all("https://s.shopee.com.br/7" in text for text in texts.values())


# ----------------------------------------------------------------------
# Favourites, task and post from an offer
# ----------------------------------------------------------------------


def _list(shopee, offers):
    page = OfferPage(offers=offers, page=1, has_next_page=False)
    with patch("apps.radar.services.ShopeeAffiliateClient.search_offers", return_value=page):
        services.list_offers(shopee, services.OfferFilters())


def test_favourite_survives_the_offer_leaving_the_listing(workspace, owner, shopee):
    _list(shopee, [_offer(11, price="99.9")])
    client = _client(owner)

    response = client.post(_url("offer_favorite", workspace), {"item_id": "11"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert b'aria-pressed="true"' in response.content

    cache.clear()
    page = client.get(_url("shopee_favorites", workspace))
    assert page.status_code == 200
    assert b"Product 11" in page.content
    assert services.find_offer(shopee, 11).price == Decimal("99.9")

    client.post(_url("offer_favorite", workspace), {"item_id": "11"}, HTTP_HX_REQUEST="true")
    assert not SavedOffer.objects.exists()


def test_cannot_favourite_an_offer_that_was_never_listed(workspace, owner, shopee):
    response = _client(owner).post(_url("offer_favorite", workspace), {"item_id": "424242"}, HTTP_HX_REQUEST="true")

    assert response.status_code == 204
    assert "showToast" in response["HX-Trigger"]
    assert not SavedOffer.objects.exists()


def test_favourites_are_per_workspace(workspace, shopee):
    other = _workspace("Other")
    OpportunityConnection.objects.create(
        workspace=other,
        source=OpportunityConnection.Source.SHOPEE_AFFILIATE,
        credentials={"app_id": "1", "secret": "x"},
    )
    SavedOffer.objects.create(workspace=workspace, source=shopee.source, item_id=5, data=offer_to_dict(_offer(5)))
    intruder = _client(_user("intruder@example.com", other, Role.OWNER))

    page = intruder.get(_url("shopee_favorites", other))

    assert b"Product 5" not in page.content
    assert intruder.post(_url("offer_task", other), {"prefill": "1", "item_id": "5"}).status_code == 204
    assert not Task.objects.exists()


def _fake_image(self, url):
    """The Shopee CDN, answering with a real PNG so the media library's validation runs."""
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "orange").save(buffer, format="PNG")
    return buffer.getvalue(), "image/png"


def test_create_task_opens_the_task_window_filled_from_the_offer(workspace, owner, shopee):
    _list(shopee, [_offer(21)])

    response = _client(owner).post(
        _url("offer_task", workspace),
        {"prefill": "1", "item_id": "21", "caption": "Look at this"},
        HTTP_HX_REQUEST="true",
    )

    body = response.content.decode()
    assert response.status_code == 200 and "data-standalone" in body
    assert 'value="Post offer: Product 21"' in body
    assert "Look at this</textarea>" in body
    assert "https://s.shopee.com.br/21" in body
    assert f'hx-post="{_url("offer_task", workspace)}"' in body
    assert 'name="item_id" value="21"' in body
    assert not Task.objects.exists()


def test_art_request_from_an_offer_has_the_formats_the_image_and_a_draft_post(workspace, owner, shopee):
    _list(shopee, [_offer(21)])
    with patch("apps.radar.services.ShopeeAffiliateClient.download_image", _fake_image):
        response = _client(owner).post(
            _url("offer_task", workspace),
            {
                "item_id": "21",
                "title": "Post offer: Product 21",
                "request_art": "1",
                "briefing": "Brief",
                "make_post": "1",
                "post_caption": "Look at this",
                "formats": ["ig_feed_portrait", "ig_story"],
            },
            HTTP_HX_REQUEST="true",
        )

    task = Task.objects.get(workspace=workspace)
    assert "taskCreated" in response["HX-Trigger"]
    assert task.post_caption == "Look at this"
    assert task.briefing == "Brief"
    assert task.post.caption == "Look at this"
    media = PostMedia.objects.get(post=task.post)
    assert media.media_asset.workspace_id == workspace.id
    assert media.media_asset.source == "shopee"
    items = list(task.checklist.order_by("position"))
    assert [i.format_key for i in items] == ["ig_feed_portrait", "ig_story", ""]
    assert items[-1].media_asset_id == media.media_asset_id


def test_a_plain_task_from_an_offer_keeps_the_image_and_the_offer_text(workspace, owner, shopee):
    _list(shopee, [_offer(22)])
    with patch("apps.radar.services.ShopeeAffiliateClient.download_image", _fake_image):
        _client(owner).post(
            _url("offer_task", workspace), {"item_id": "22", "title": "Post it"}, HTTP_HX_REQUEST="true"
        )

    task = Task.objects.get(workspace=workspace)
    assert "https://s.shopee.com.br/22" in task.post.caption
    assert task.checklist.get().media_asset_id is not None


def test_art_request_from_an_offer_needs_a_format(workspace, owner, shopee):
    _list(shopee, [_offer(21)])

    response = _client(owner).post(
        _url("offer_task", workspace), {"item_id": "21", "title": "Arts", "request_art": "1"}, HTTP_HX_REQUEST="true"
    )

    assert "Choose at least one format." in response["HX-Trigger"]
    assert not Task.objects.exists()


def test_offer_brief_starts_with_the_summary():
    brief = services.offer_brief(_offer(3, price="50", commission="5", rate="0.10"))

    assert brief.splitlines()[0] == "R$ 50.00 · Commission 10% · ~R$ 5.00"
    assert "https://s.shopee.com.br/3" in brief


def test_schedule_post_opens_the_draft_in_the_composer(workspace, owner, shopee):
    _list(shopee, [_offer(31)])
    with (
        patch("apps.radar.services.ShopeeAffiliateClient.download_image", _fake_image),
    ):
        response = _client(owner).post(_url("offer_post", workspace), {"item_id": "31"}, HTTP_HX_REQUEST="true")

    post = Post.objects.get(workspace=workspace)
    assert response["HX-Redirect"] == reverse(
        "composer:compose_edit", kwargs={"workspace_id": workspace.id, "post_id": post.id}
    )
    assert "https://s.shopee.com.br/31" in post.caption


def test_image_outside_shopee_cdn_is_never_fetched(shopee):
    client = services.shopee_client(shopee)
    with patch.object(client, "_request") as request, pytest.raises(ValueError):
        client.download_image("https://169.254.169.254/latest/meta-data")

    request.assert_not_called()


def test_viewers_cannot_create_tasks_or_posts(workspace, shopee):
    _list(shopee, [_offer(41)])
    viewer = _client(_user("viewer@example.com", workspace, Role.VIEWER))

    assert viewer.post(_url("offer_task", workspace), {"prefill": "1", "item_id": "41"}).status_code == 403
    assert viewer.post(_url("offer_post", workspace), {"item_id": "41"}).status_code == 403
    assert viewer.post(_url("offer_favorite", workspace), {"item_id": "41"}).status_code == 403


def test_video_search_terms_are_the_first_words_of_the_title():
    assert services.search_terms("Kit Cobre-Leito Ideale, Super King 6 Peças") == [
        "kit cobre leito ideale",
        "kit cobre leito",
        "kit cobre",
    ]
    assert services.search_terms("Cropped") == ["cropped"]


def test_dialog_amounts_are_rounded_to_cents():
    payload = services.share_payload([_offer(9, commission="57.9054", rate="0.15")])["9"]

    assert payload["commission"] == "Commission 15% · ~R$ 57.91"
    assert payload["terms"] == ["product 9"]
