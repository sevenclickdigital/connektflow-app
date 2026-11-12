"""Shopee Affiliate Open API client: product offers and the affiliate's conversions.

Not a social platform — nothing is published here — so it is not in
``PROVIDER_REGISTRY``. ``apps.radar`` uses it to list offers the workspace can
share with its own affiliate link and to report the orders those links brought.

The API is a single GraphQL endpoint per region. Every request is signed with
``SHA256(app_id + timestamp + payload + secret)`` over the exact body bytes,
so the body is serialized once and sent verbatim.

The API answers HTTP 200 even on failure, with ``errors[].extensions.code``:

* ``10000`` — "System Error". Intermittent on Shopee's side: the same query
  succeeds and fails on consecutive calls, keyword searches most of all. One
  retry after a short pause clears most of them.
* ``10010`` / ``11001`` — malformed query or parameters (our bug, not the user's).
* ``10020`` — invalid signature, credential or expired timestamp: the App ID
  or Secret is wrong.
* ``10030`` — rate limited.
* ``10031``–``10035`` — the affiliate account has no Open API access (denied,
  frozen, blacklisted).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from .base import HttpProvider
from .exceptions import APIError, OAuthError, ProviderError, RateLimitError

logger = logging.getLogger(__name__)

# Endpoints are fixed per region and never taken from user input, so a
# connection cannot be pointed at an arbitrary host.
REGION_ENDPOINTS = {
    "br": "https://open-api.affiliate.shopee.com.br/graphql",
}
DEFAULT_REGION = "br"

PAGE_SIZE = 50

# ``productOfferV2(sortType:)`` values.
SORT_RELEVANCE = 1
SORT_SALES = 2
SORT_PRICE_DESC = 3
SORT_PRICE_ASC = 4
SORT_COMMISSION_RATE = 5

TRANSIENT_RETRY_DELAY_SECONDS = 1.0

# Product images are only ever fetched from Shopee's own CDN, so an image URL
# can never make the server request an arbitrary host.
IMAGE_HOST_SUFFIXES = (".shopee.com.br", ".susercontent.com")
IMAGE_MAX_BYTES = 10 * 1024 * 1024

_OFFER_FIELDS = (
    "itemId productName imageUrl shopName price priceMin priceMax priceDiscountRate "
    "ratingStar sales commissionRate commission sellerCommissionRate shopeeCommissionRate "
    "productLink offerLink productCatIds"
)

# Variables instead of string interpolation: the keyword comes from the user.
OFFERS_QUERY = (
    "query($keyword:String,$categoryId:Int,$sortType:Int,$page:Int,$limit:Int){"
    "productOfferV2(keyword:$keyword,productCatId:$categoryId,sortType:$sortType,page:$page,limit:$limit){"
    f"nodes{{{_OFFER_FIELDS}}} pageInfo{{page limit hasNextPage}}}}}}"
)

# ``conversionReport`` only reaches back this far ("can only query data for the
# last 3 months"), and returns at most this many rows per page.
REPORT_MAX_DAYS = 90
REPORT_PAGE_SIZE = 500
# A guard against a scroll that never ends, not a limit anyone should reach.
REPORT_MAX_PAGES = 20

_CONVERSION_FIELDS = (
    "purchaseTime conversionId netCommission buyerType referrer "
    "orders{orderId orderStatus items{itemId itemName shopName imageUrl qty actualAmount itemTotalCommission}}"
)
# Times are Int64, which Shopee only accepts as JSON strings. The follow-up
# pages repeat the same window plus the ``scrollId`` of the previous page (valid
# for 30 seconds); an explicit null ``scrollId`` on the first page is rejected.
CONVERSIONS_QUERY = (
    "query($start:Int64,$end:Int64,$limit:Int){"
    "conversionReport(purchaseTimeStart:$start,purchaseTimeEnd:$end,limit:$limit){"
    f"nodes{{{_CONVERSION_FIELDS}}} pageInfo{{hasNextPage scrollId}}}}}}"
)
CONVERSIONS_SCROLL_QUERY = (
    "query($start:Int64,$end:Int64,$limit:Int,$scrollId:String){"
    "conversionReport(purchaseTimeStart:$start,purchaseTimeEnd:$end,limit:$limit,scrollId:$scrollId){"
    f"nodes{{{_CONVERSION_FIELDS}}} pageInfo{{hasNextPage scrollId}}}}}}"
)

# ``orderStatus`` values.
ORDER_PENDING = "PENDING"
ORDER_COMPLETED = "COMPLETED"
ORDER_CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class Offer:
    """One product as the affiliate sees it. Money in BRL, rates as fractions (0.13 = 13%)."""

    item_id: int
    name: str
    image_url: str
    shop_name: str
    price: Decimal
    price_min: Decimal
    price_max: Decimal
    discount_rate: int
    rating: Decimal | None
    sales: int
    commission_rate: Decimal
    commission: Decimal
    seller_commission_rate: Decimal
    shopee_commission_rate: Decimal
    product_link: str
    offer_link: str
    category_ids: tuple[int, ...]

    @property
    def commission_percent(self) -> Decimal:
        return self.commission_rate * 100

    @property
    def share_link(self) -> str:
        """The tracked short link when Shopee issued one, else the plain product page."""
        return self.offer_link or self.product_link


@dataclass(frozen=True)
class OfferPage:
    offers: list[Offer]
    page: int
    has_next_page: bool


@dataclass(frozen=True)
class Conversion:
    """One order bought through the affiliate's link. Money in BRL.

    ``commission`` is what the affiliate keeps (Shopee's ``netCommission``);
    a cancelled order reports 0. ``item_name``/``image_url``/``shop_name`` are
    the order's first item, ``item_count`` how many distinct items it has.
    """

    order_id: str
    purchased_at: datetime
    status: str
    commission: Decimal
    amount: Decimal
    quantity: int
    referrer: str
    buyer_type: str
    item_id: int
    item_name: str
    item_count: int
    image_url: str
    shop_name: str


@dataclass(frozen=True)
class ConversionReport:
    conversions: list[Conversion]
    # False when the scroll was cut at ``REPORT_MAX_PAGES``.
    complete: bool = True


def _decimal(value, default: Decimal | None = Decimal(0)) -> Decimal | None:
    """Shopee sends numbers as strings ("129.6"); empty or junk becomes ``default``."""
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def offer_to_dict(offer: Offer) -> dict:
    """JSON-safe snapshot of an offer (Decimals as strings), for favourites."""
    data = asdict(offer)
    return {k: (str(v) if isinstance(v, Decimal) else list(v) if isinstance(v, tuple) else v) for k, v in data.items()}


def offer_from_dict(data: dict) -> Offer:
    """Inverse of :func:`offer_to_dict`; tolerant of keys added or dropped since the snapshot."""
    decimals = {f.name for f in fields(Offer) if "Decimal" in str(f.type)}
    values: dict[str, Any] = {}
    for f in fields(Offer):
        raw = data.get(f.name)
        if f.name in decimals:
            values[f.name] = _decimal(raw, default=None if f.name == "rating" else Decimal(0))
        elif f.name == "category_ids":
            values[f.name] = tuple(raw or ())
        elif f.name in ("item_id", "discount_rate", "sales"):
            values[f.name] = _int(raw)
        else:
            values[f.name] = raw or ""
    return Offer(**values)


def is_shopee_image_url(url: str) -> bool:
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host.endswith(suffix) for suffix in IMAGE_HOST_SUFFIXES)


def parse_offer(node: dict) -> Offer:
    rating = _decimal(node.get("ratingStar"), default=None)
    return Offer(
        item_id=_int(node.get("itemId")),
        name=(node.get("productName") or "").strip(),
        image_url=node.get("imageUrl") or "",
        shop_name=node.get("shopName") or "",
        price=_decimal(node.get("price")) or Decimal(0),
        price_min=_decimal(node.get("priceMin")) or Decimal(0),
        price_max=_decimal(node.get("priceMax")) or Decimal(0),
        discount_rate=_int(node.get("priceDiscountRate")),
        # Shopee reports an unrated product as 0, which is not a rating.
        rating=rating if rating else None,
        sales=_int(node.get("sales")),
        commission_rate=_decimal(node.get("commissionRate")) or Decimal(0),
        commission=_decimal(node.get("commission")) or Decimal(0),
        seller_commission_rate=_decimal(node.get("sellerCommissionRate")) or Decimal(0),
        shopee_commission_rate=_decimal(node.get("shopeeCommissionRate")) or Decimal(0),
        product_link=node.get("productLink") or "",
        offer_link=node.get("offerLink") or "",
        category_ids=tuple(c for c in (_int(c) for c in node.get("productCatIds") or ()) if c),
    )


def parse_conversions(node: dict) -> list[Conversion]:
    """One :class:`Conversion` per order in a ``conversionReport`` node.

    Shopee reports the net commission per node; when a node holds several
    orders it is shared out by each order's gross item commission.
    """
    orders = [o for o in node.get("orders") or [] if isinstance(o, dict)]
    net = _decimal(node.get("netCommission")) or Decimal(0)
    purchased_at = datetime.fromtimestamp(_int(node.get("purchaseTime")), tz=UTC)
    gross = [
        sum(
            (_decimal(i.get("itemTotalCommission")) or Decimal(0) for i in o.get("items") or [] if isinstance(i, dict)),
            Decimal(0),
        )
        for o in orders
    ]
    total_gross = sum(gross, Decimal(0))
    conversions = []
    for order, order_gross in zip(orders, gross, strict=True):
        items = [i for i in order.get("items") or [] if isinstance(i, dict)]
        first = items[0] if items else {}
        # A lone order takes it all, even when Shopee reports no item commission.
        share = order_gross / total_gross if total_gross else Decimal(len(orders) == 1)
        commission = net * share
        conversions.append(
            Conversion(
                order_id=str(order.get("orderId") or ""),
                purchased_at=purchased_at,
                status=str(order.get("orderStatus") or "").upper(),
                commission=commission,
                amount=sum((_decimal(i.get("actualAmount")) or Decimal(0) for i in items), Decimal(0)),
                quantity=sum(_int(i.get("qty")) for i in items),
                referrer=node.get("referrer") or "",
                buyer_type=node.get("buyerType") or "",
                item_id=_int(first.get("itemId")),
                item_name=(first.get("itemName") or "").strip(),
                item_count=len(items),
                image_url=first.get("imageUrl") or "",
                shop_name=first.get("shopName") or "",
            )
        )
    return conversions


class ShopeeAffiliateClient(HttpProvider):
    platform_name = "Shopee Affiliate"

    def __init__(self, app_id: str, secret: str, region: str = DEFAULT_REGION):
        if region not in REGION_ENDPOINTS:
            raise ValueError(f"Unknown Shopee Affiliate region: {region!r}")
        self.app_id = app_id
        self.secret = secret
        self.endpoint = REGION_ENDPOINTS[region]

    def _authorization(self, timestamp: str, payload: str) -> str:
        signature = hashlib.sha256(f"{self.app_id}{timestamp}{payload}{self.secret}".encode()).hexdigest()
        return f"SHA256 Credential={self.app_id}, Timestamp={timestamp}, Signature={signature}"

    def _post(self, query: str, variables: dict) -> dict:
        payload = json.dumps({"query": query, "variables": variables}, separators=(",", ":"))
        response = self._request(
            "POST",
            self.endpoint,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._authorization(str(int(time.time())), payload),
            },
            data=payload.encode(),
        )
        body = self._safe_json(response)
        if not isinstance(body, dict):
            raise APIError(f"{self.platform_name}: unreadable response", status_code=response.status_code)
        errors = body.get("errors")
        if errors:
            raise self._error_for_graphql(errors[0] if isinstance(errors[0], dict) else {})
        return body.get("data") or {}

    def _error_for_graphql(self, error: dict) -> ProviderError:
        extensions = error.get("extensions") or {}
        code = _int(extensions.get("code"))
        detail = extensions.get("message") or error.get("message") or "unknown error"
        message = f"{self.platform_name} error {code}: {detail}"
        raw = {"errors": [error]}
        if code == 10020:
            return OAuthError(message, platform=self.platform_name, raw_response=raw, retryable=False)
        if code == 10030:
            return RateLimitError(message, platform=self.platform_name, raw_response=raw)
        if 10031 <= code <= 10035:
            return APIError(message, status_code=403, platform=self.platform_name, raw_response=raw, retryable=False)
        if code in (10010, 11001):
            return APIError(message, status_code=400, platform=self.platform_name, raw_response=raw, retryable=False)
        return APIError(message, status_code=503, platform=self.platform_name, raw_response=raw)

    def _query(self, query: str, variables: dict) -> dict:
        """Run ``query``, retrying once on Shopee's intermittent "System Error"."""
        try:
            return self._post(query, variables)
        except APIError as exc:
            if not exc.retryable:
                raise
            logger.info("Shopee Affiliate transient failure, retrying once: %s", exc)
            time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
            return self._post(query, variables)

    def search_offers(
        self,
        *,
        keyword: str = "",
        category_id: int | None = None,
        sort_type: int = SORT_RELEVANCE,
        page: int = 1,
        limit: int = PAGE_SIZE,
    ) -> OfferPage:
        variables: dict = {"sortType": sort_type, "page": page, "limit": limit}
        # An explicit null category makes Shopee fail; leave unset filters out.
        if keyword:
            variables["keyword"] = keyword
        if category_id:
            variables["categoryId"] = category_id
        data = self._query(OFFERS_QUERY, variables).get("productOfferV2") or {}
        page_info = data.get("pageInfo") or {}
        return OfferPage(
            offers=[parse_offer(node) for node in data.get("nodes") or [] if isinstance(node, dict)],
            page=_int(page_info.get("page"), page),
            has_next_page=bool(page_info.get("hasNextPage")),
        )

    def conversions(self, start: datetime, end: datetime) -> ConversionReport:
        """Every order bought between ``start`` and ``end`` (by purchase time).

        ``start`` must be within the last ``REPORT_MAX_DAYS`` days; Shopee
        refuses older windows with error 11001.
        """
        variables: dict = {
            "start": str(int(start.timestamp())),
            "end": str(int(end.timestamp())),
            "limit": REPORT_PAGE_SIZE,
        }
        conversions: list[Conversion] = []
        query = CONVERSIONS_QUERY
        for _page in range(REPORT_MAX_PAGES):
            data = self._query(query, variables).get("conversionReport") or {}
            for node in data.get("nodes") or []:
                if isinstance(node, dict):
                    conversions.extend(parse_conversions(node))
            page_info = data.get("pageInfo") or {}
            scroll_id = page_info.get("scrollId")
            if not page_info.get("hasNextPage") or not scroll_id:
                return ConversionReport(conversions)
            query = CONVERSIONS_SCROLL_QUERY
            variables = {**variables, "scrollId": scroll_id}
        logger.warning("Shopee Affiliate conversion report cut at %s pages", REPORT_MAX_PAGES)
        return ConversionReport(conversions, complete=False)

    def download_image(self, url: str) -> tuple[bytes, str]:
        """Fetch a product image from Shopee's CDN. Returns ``(content, content_type)``.

        Raises ``ValueError`` for a URL outside the CDN or a body over
        ``IMAGE_MAX_BYTES``, and the usual provider errors for HTTP failures.
        """
        if not is_shopee_image_url(url):
            raise ValueError(f"Not a Shopee image URL: {url!r}")
        response = self._request("GET", url)
        if len(response.content) > IMAGE_MAX_BYTES:
            raise ValueError("Shopee image is larger than the import limit")
        return response.content, response.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()

    def verify(self) -> None:
        """Raise unless the App ID and Secret are accepted. Costs one tiny query."""
        self.search_offers(limit=1)
