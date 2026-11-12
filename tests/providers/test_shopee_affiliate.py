"""Shopee Affiliate client: request signing, payload mapping and error classification."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from providers.exceptions import APIError, OAuthError, RateLimitError
from providers.shopee_affiliate import SORT_SALES, ShopeeAffiliateClient, parse_conversions

NODE = {
    "itemId": 23098822163,
    "productName": " Vestido floral ",
    "price": "129.6",
    "priceMin": "129.6",
    "priceMax": "129.6",
    "commissionRate": "0.13",
    "sellerCommissionRate": "0.1",
    "shopeeCommissionRate": "0.03",
    "commission": "16.848",
    "sales": 2,
    "ratingStar": "4.6",
    "priceDiscountRate": 55,
    "imageUrl": "https://cf.shopee.com.br/file/abc",
    "shopName": "MISSORDBR",
    "productLink": "https://shopee.com.br/product/1/2",
    "offerLink": "https://s.shopee.com.br/5VWq7SGyqa",
    "productCatIds": [100017, 100104, 0],
}


def _response(body):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = body
    return response


def _ok(nodes=(NODE,), has_next=True):
    return _response(
        {"data": {"productOfferV2": {"nodes": list(nodes), "pageInfo": {"page": 1, "hasNextPage": has_next}}}}
    )


def _error(code, message="boom"):
    return _response({"errors": [{"message": message, "extensions": {"code": code, "message": message}}]})


@pytest.fixture
def client():
    return ShopeeAffiliateClient("18340070022", "s3cret")


def test_signs_the_exact_body_it_sends(client):
    with patch.object(client, "_request", return_value=_ok()) as request, patch("time.time", return_value=1700000000):
        client.search_offers(keyword="cama")

    kwargs = request.call_args.kwargs
    body = kwargs["data"].decode()
    expected = hashlib.sha256(f"18340070022{1700000000}{body}s3cret".encode()).hexdigest()
    assert kwargs["headers"]["Authorization"] == (
        f"SHA256 Credential=18340070022, Timestamp=1700000000, Signature={expected}"
    )
    assert request.call_args.args == ("POST", "https://open-api.affiliate.shopee.com.br/graphql")


def test_user_keyword_travels_as_a_variable_and_unset_filters_are_left_out(client):
    with patch.object(client, "_request", return_value=_ok()) as request:
        client.search_offers(keyword='x"){evil}', sort_type=SORT_SALES, page=2)

    payload = json.loads(request.call_args.kwargs["data"])
    assert 'x"){evil}' not in payload["query"]
    assert payload["variables"] == {"keyword": 'x"){evil}', "sortType": SORT_SALES, "page": 2, "limit": 50}


def test_maps_shopee_strings_into_typed_offers(client):
    with patch.object(client, "_request", return_value=_ok()):
        page = client.search_offers()

    offer = page.offers[0]
    assert page.has_next_page is True
    assert offer.name == "Vestido floral"
    assert offer.price == Decimal("129.6")
    assert offer.commission == Decimal("16.848")
    assert offer.commission_percent == Decimal("13")
    assert offer.rating == Decimal("4.6")
    assert offer.discount_rate == 55
    assert offer.category_ids == (100017, 100104)
    assert offer.share_link == "https://s.shopee.com.br/5VWq7SGyqa"


def test_unrated_product_has_no_rating_and_falls_back_to_product_link(client):
    node = {**NODE, "ratingStar": "0", "offerLink": ""}
    with patch.object(client, "_request", return_value=_ok(nodes=[node])):
        offer = client.search_offers().offers[0]

    assert offer.rating is None
    assert offer.share_link == "https://shopee.com.br/product/1/2"


@pytest.mark.parametrize(
    ("code", "exc_type", "retryable"),
    [
        (10020, OAuthError, False),
        (10030, RateLimitError, True),
        (10031, APIError, False),
        (10010, APIError, False),
    ],
)
def test_classifies_graphql_errors_answered_with_http_200(client, code, exc_type, retryable):
    with patch.object(client, "_request", return_value=_error(code)) as request, pytest.raises(exc_type) as raised:
        client.search_offers()

    assert raised.value.retryable is retryable
    assert request.call_count == 1


def test_retries_shopee_system_error_once(client):
    with (
        patch.object(client, "_request", side_effect=[_error(10000, "System Error"), _ok()]) as request,
        patch("providers.shopee_affiliate.time.sleep"),
    ):
        page = client.search_offers()

    assert request.call_count == 2
    assert len(page.offers) == 1


def test_gives_up_after_the_second_system_error(client):
    with (
        patch.object(client, "_request", return_value=_error(10000, "System Error")) as request,
        patch("providers.shopee_affiliate.time.sleep"),
        pytest.raises(APIError) as raised,
    ):
        client.search_offers()

    assert request.call_count == 2
    assert raised.value.status_code == 503


def test_unknown_region_is_refused():
    with pytest.raises(ValueError):
        ShopeeAffiliateClient("1", "s", region="xx")


def _conversion_node(order_id, *, status="COMPLETED", net="3.5", items=None, purchase_time=1794399650):
    return {
        "purchaseTime": purchase_time,
        "conversionId": 1,
        "netCommission": net,
        "buyerType": "NEW",
        "referrer": "WhatsApp",
        "orders": [
            {
                "orderId": order_id,
                "orderStatus": status,
                "items": items
                or [
                    {
                        "itemId": 7,
                        "itemName": " Guarda-chuva ",
                        "shopName": "Loja",
                        "imageUrl": "https://cf.shopee.com.br/file/u",
                        "qty": 2,
                        "actualAmount": "27.19",
                        "itemTotalCommission": net,
                    }
                ],
            }
        ],
    }


def _report(nodes, *, has_next=False, scroll_id=""):
    return _response(
        {"data": {"conversionReport": {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "scrollId": scroll_id}}}}
    )


def test_conversions_follow_the_scroll_with_the_same_window(client):
    pages = [_report([_conversion_node("A")], has_next=True, scroll_id="s1"), _report([_conversion_node("B")])]
    start, end = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
    with patch.object(client, "_request", side_effect=pages) as request:
        report = client.conversions(start, end)

    first, second = (json.loads(c.kwargs["data"]) for c in request.call_args_list)
    # Int64 times only pass as strings; the first page sends no scrollId at all.
    assert first["variables"] == {"start": str(int(start.timestamp())), "end": str(int(end.timestamp())), "limit": 500}
    assert second["variables"] == {**first["variables"], "scrollId": "s1"}
    assert [c.order_id for c in report.conversions] == ["A", "B"]
    assert report.complete is True


def test_conversion_maps_the_order_and_its_first_item(client):
    with patch.object(client, "_request", return_value=_report([_conversion_node("A")])):
        conversion = client.conversions(
            datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
        ).conversions[0]

    assert conversion.status == "COMPLETED"
    assert conversion.commission == Decimal("3.5")
    assert conversion.amount == Decimal("27.19")
    assert conversion.quantity == 2
    assert conversion.item_name == "Guarda-chuva"
    assert conversion.purchased_at.timestamp() == 1794399650


def test_net_commission_of_a_node_is_shared_by_its_orders():
    node = _conversion_node("A", net="9")
    item = {"qty": 1, "actualAmount": "10", "itemTotalCommission": "1"}
    node["orders"] = [
        {"orderId": "A", "orderStatus": "PENDING", "items": [{**item, "itemTotalCommission": "2"}]},
        {"orderId": "B", "orderStatus": "PENDING", "items": [item]},
    ]

    assert [c.commission for c in parse_conversions(node)] == [Decimal(6), Decimal(3)]
