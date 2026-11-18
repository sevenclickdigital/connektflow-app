"""Radar business logic: connecting sources and turning their data into offers to share."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.core.cache import cache
from django.template.defaultfilters import floatformat
from django.utils import formats, timezone
from django.utils.functional import Promise
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext

from providers.exceptions import APIError, OAuthError, ProviderError, RateLimitError
from providers.shopee_affiliate import (
    SORT_COMMISSION_RATE,
    SORT_PRICE_ASC,
    SORT_PRICE_DESC,
    SORT_RELEVANCE,
    SORT_SALES,
    Offer,
    OfferPage,
    ShopeeAffiliateClient,
    offer_from_dict,
    offer_to_dict,
)

from .models import OpportunityConnection, SavedOffer

StrOrPromise = str | Promise

OFFERS_CACHE_SECONDS = 10 * 60
# How long a listed offer stays actionable (favourite, task, post) by id alone.
OFFER_MEMORY_SECONDS = 6 * 60 * 60
MAX_PAGES = 5

# Shopee Brazil level-1 categories (``productCatId``).
SHOPEE_CATEGORIES: list[tuple[int, StrOrPromise]] = [
    (100630, gettext_lazy("Beauty & personal care")),
    (100001, gettext_lazy("Health")),
    (100636, gettext_lazy("Home & kitchen")),
    (100010, gettext_lazy("Home appliances")),
    (100013, gettext_lazy("Mobile & accessories")),
    (100644, gettext_lazy("Computers")),
    (100635, gettext_lazy("Cameras & security")),
    (100634, gettext_lazy("Games")),
    (100017, gettext_lazy("Women's fashion")),
    (100011, gettext_lazy("Men's fashion")),
    (100633, gettext_lazy("Kids' fashion")),
    (100012, gettext_lazy("Shoes")),
    (100016, gettext_lazy("Bags & backpacks")),
    (100009, gettext_lazy("Fashion accessories")),
    (100015, gettext_lazy("Luggage & travel")),
    (100632, gettext_lazy("Mom & baby")),
    (100631, gettext_lazy("Pets")),
    (100637, gettext_lazy("Sports & outdoors")),
    (100629, gettext_lazy("Food & drinks")),
    (100639, gettext_lazy("Hobbies & crafts")),
    (100643, gettext_lazy("Books")),
    (100638, gettext_lazy("Stationery & office")),
    (100640, gettext_lazy("Car accessories")),
    (102187, gettext_lazy("Automotive")),
    (100641, gettext_lazy("Motorcycles & accessories")),
    (100642, gettext_lazy("Services & others")),
]

SORT_CHOICES: list[tuple[str, StrOrPromise]] = [
    ("commission", gettext_lazy("Highest commission (R$)")),
    ("commission_rate", gettext_lazy("Highest commission (%)")),
    ("discount", gettext_lazy("Biggest discount")),
    ("sales", gettext_lazy("Best selling")),
    ("rating", gettext_lazy("Best rated")),
    ("price_asc", gettext_lazy("Lowest price")),
    ("price_desc", gettext_lazy("Highest price")),
]
DEFAULT_SORT = "commission"

# The order Shopee is asked for, so the page fetched already leans the right
# way; the loaded offers are then sorted locally for the exact order.
_API_SORT = {
    "commission": SORT_COMMISSION_RATE,
    "commission_rate": SORT_COMMISSION_RATE,
    "sales": SORT_SALES,
    "price_asc": SORT_PRICE_ASC,
    "price_desc": SORT_PRICE_DESC,
}

_LOCAL_SORT: dict[str, tuple[Callable[[Offer], object], bool]] = {
    "commission": (lambda o: o.commission, True),
    "commission_rate": (lambda o: o.commission_rate, True),
    "discount": (lambda o: o.discount_rate, True),
    "sales": (lambda o: o.sales, True),
    "rating": (lambda o: o.rating or Decimal(0), True),
    "price_asc": (lambda o: o.price, False),
    "price_desc": (lambda o: o.price, True),
}


# Where to look for videos of a product: (icon key, label, search URL prefix).
# Instagram has no public keyword-search URL other than this one.
VIDEO_PLATFORMS: list[tuple[str, str, str]] = [
    ("tiktok", "TikTok", "https://www.tiktok.com/search?q="),
    ("youtube", "YouTube", "https://www.youtube.com/results?search_query="),
    ("instagram", "Instagram", "https://www.instagram.com/explore/search/keyword/?q="),
    ("pinterest", "Pinterest", "https://www.pinterest.com/search/pins/?q="),
]

# The share texts built by share_texts(), as offered in the dialog.
MESSAGE_TEMPLATES: list[tuple[str, StrOrPromise, StrOrPromise]] = [
    ("direct", gettext_lazy("Direct"), gettext_lazy("Full offer, with a price notice")),
    ("urgent", gettext_lazy("Urgency"), gettext_lazy("Stronger call, for groups")),
    ("simple", gettext_lazy("Simple"), gettext_lazy("Short, just the essentials")),
]


@dataclass(frozen=True)
class QuickFilter:
    key: str
    label: StrOrPromise
    match: Callable[[Offer], bool]
    sort: str


QUICK_FILTERS: list[QuickFilter] = [
    QuickFilter("discounted", gettext_lazy("On sale"), lambda o: o.discount_rate > 0, "discount"),
    QuickFilter("top_rated", gettext_lazy("Top rated"), lambda o: (o.rating or 0) >= Decimal("4.5"), "rating"),
    QuickFilter("best_selling", gettext_lazy("Best selling"), lambda o: o.sales > 0, "sales"),
    QuickFilter("top_commission", gettext_lazy("Highest commission"), lambda o: o.commission_rate > 0, "commission"),
    QuickFilter("under_20", gettext_lazy("Up to R$ 20"), lambda o: 0 < o.price <= 20, "price_asc"),
    QuickFilter("under_50", gettext_lazy("Up to R$ 50"), lambda o: 0 < o.price <= 50, "price_asc"),
    QuickFilter("under_100", gettext_lazy("Up to R$ 100"), lambda o: 0 < o.price <= 100, "price_asc"),
]
QUICK_FILTERS_BY_KEY = {q.key: q for q in QUICK_FILTERS}


@dataclass(frozen=True)
class OfferFilters:
    keyword: str = ""
    category_id: int | None = None
    sort: str = DEFAULT_SORT
    quick: str = ""
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    min_discount: int | None = None
    min_rating: Decimal | None = None
    min_sales: int | None = None
    min_commission_pct: Decimal | None = None
    pages: int = 1


@dataclass(frozen=True)
class OfferResults:
    offers: list[Offer]
    loaded: int
    has_more: bool


# ----------------------------------------------------------------------
# Connection
# ----------------------------------------------------------------------


def shopee_client(connection: OpportunityConnection) -> ShopeeAffiliateClient:
    creds = connection.credentials or {}
    return ShopeeAffiliateClient(creds.get("app_id", ""), creds.get("secret", ""), region=connection.region)


def connect_shopee(*, workspace, user, app_id: str, secret: str, region: str = "br") -> OpportunityConnection:
    """Check the credentials against Shopee, then link them to the workspace.

    Raises the provider's error when Shopee refuses them, so nothing is saved
    for a pair that cannot work.
    """
    ShopeeAffiliateClient(app_id, secret, region=region).verify()
    connection, _created = OpportunityConnection.objects.update_or_create(
        workspace=workspace,
        source=OpportunityConnection.Source.SHOPEE_AFFILIATE,
        defaults={
            "account_label": app_id,
            "credentials": {"app_id": app_id, "secret": secret},
            "region": region,
            "status": OpportunityConnection.Status.CONNECTED,
            "last_error": "",
            "last_checked_at": timezone.now(),
            "connected_by": user,
        },
    )
    return connection


def _record_health(connection: OpportunityConnection, error: ProviderError | None) -> None:
    """Keep the connection's status in step with Shopee's last answer.

    A queryset ``update`` so ``updated_at`` — part of the offers cache key — only
    moves when the credentials do.
    """
    fields: dict[str, object]
    if error is None:
        if connection.status == OpportunityConnection.Status.CONNECTED and not connection.last_error:
            return
        fields = {"status": OpportunityConnection.Status.CONNECTED, "last_error": ""}
    elif isinstance(error, OAuthError):
        fields = {"status": OpportunityConnection.Status.ERROR, "last_error": str(error)[:1000]}
    else:
        return
    fields["last_checked_at"] = timezone.now()
    OpportunityConnection.objects.filter(pk=connection.pk).update(**fields)
    for key, value in fields.items():
        setattr(connection, key, value)


# ----------------------------------------------------------------------
# Offers
# ----------------------------------------------------------------------


def _page_cache_key(connection: OpportunityConnection, filters: OfferFilters, page: int) -> str:
    params = json.dumps(
        {"k": filters.keyword, "c": filters.category_id, "s": _API_SORT.get(filters.sort, SORT_RELEVANCE), "p": page},
        sort_keys=True,
    )
    digest = hashlib.sha256(params.encode()).hexdigest()[:24]
    version = int(connection.updated_at.timestamp()) if connection.updated_at else 0
    return f"radar:shopee:{connection.pk}:{version}:{digest}"


def _fetch_page(connection: OpportunityConnection, filters: OfferFilters, page: int) -> OfferPage:
    key = _page_cache_key(connection, filters, page)
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        result = shopee_client(connection).search_offers(
            keyword=filters.keyword,
            category_id=filters.category_id,
            sort_type=_API_SORT.get(filters.sort, SORT_RELEVANCE),
            page=page,
        )
    except ProviderError as exc:
        _record_health(connection, exc)
        raise
    _record_health(connection, None)
    cache.set(key, result, OFFERS_CACHE_SECONDS)
    return result


def _passes(offer: Offer, filters: OfferFilters) -> bool:
    quick = QUICK_FILTERS_BY_KEY.get(filters.quick)
    if quick and not quick.match(offer):
        return False
    if filters.min_price is not None and offer.price < filters.min_price:
        return False
    if filters.max_price is not None and offer.price > filters.max_price:
        return False
    if filters.min_discount is not None and offer.discount_rate < filters.min_discount:
        return False
    if filters.min_rating is not None and (offer.rating or 0) < filters.min_rating:
        return False
    if filters.min_sales is not None and offer.sales < filters.min_sales:
        return False
    return not (filters.min_commission_pct is not None and offer.commission_rate * 100 < filters.min_commission_pct)


def effective_sort(filters: OfferFilters) -> str:
    """A quick filter brings its own order (e.g. "Up to R$ 20" → lowest price first)."""
    quick = QUICK_FILTERS_BY_KEY.get(filters.quick)
    return quick.sort if quick else filters.sort


def list_offers(connection: OpportunityConnection, filters: OfferFilters) -> OfferResults:
    """Load ``filters.pages`` pages from Shopee (cached), then filter and sort them.

    Raises :class:`ProviderError` when Shopee fails; see :func:`describe_error`.
    """
    pages = max(1, min(filters.pages, MAX_PAGES))
    seen: set[int] = set()
    loaded: list[Offer] = []
    has_more = False
    for page in range(1, pages + 1):
        result = _fetch_page(connection, filters, page)
        for offer in result.offers:
            if offer.item_id not in seen:
                seen.add(offer.item_id)
                loaded.append(offer)
        has_more = result.has_next_page
        if not has_more:
            break
    _remember_offers(connection, loaded)
    key, reverse = _LOCAL_SORT.get(effective_sort(filters), _LOCAL_SORT[DEFAULT_SORT])
    offers = sorted((o for o in loaded if _passes(o, filters)), key=key, reverse=reverse)  # type: ignore[arg-type]
    return OfferResults(offers=offers, loaded=len(loaded), has_more=has_more and pages < MAX_PAGES)


# ----------------------------------------------------------------------
# One offer: the actions on a card (favourite, task, post)
# ----------------------------------------------------------------------


class OfferUnavailableError(Exception):
    """The offer is neither in a recent listing nor in the favourites."""


def _offer_cache_key(connection: OpportunityConnection, item_id: int) -> str:
    return f"radar:shopee:{connection.pk}:item:{item_id}"


def _remember_offers(connection: OpportunityConnection, offers: list[Offer]) -> None:
    """Keep every listed offer addressable by id, so actions only take an id from the browser.

    The browser never sends offer data (and so never an image URL for the
    server to fetch); the server answers from what Shopee itself listed.
    """
    cache.set_many({_offer_cache_key(connection, o.item_id): o for o in offers}, OFFER_MEMORY_SECONDS)


def find_offer(connection: OpportunityConnection, item_id: int) -> Offer:
    """The offer as last listed for this connection, else its favourite snapshot."""
    offer = cache.get(_offer_cache_key(connection, item_id))
    if offer is not None:
        return offer
    saved = (
        SavedOffer.objects.for_workspace(connection.workspace_id)
        .filter(source=connection.source, item_id=item_id)
        .first()
    )
    if saved is None:
        raise OfferUnavailableError(item_id)
    return offer_from_dict(saved.data)


def favorite_ids(workspace_id, source: str) -> set[int]:
    return set(SavedOffer.objects.for_workspace(workspace_id).filter(source=source).values_list("item_id", flat=True))


def favorite_offers(workspace_id, source: str) -> list[tuple[Offer, SavedOffer]]:
    return [
        (offer_from_dict(saved.data), saved)
        for saved in SavedOffer.objects.for_workspace(workspace_id).filter(source=source)
    ]


def toggle_favorite(connection: OpportunityConnection, item_id: int, user) -> bool:
    """Star or unstar an offer. Returns whether it is a favourite afterwards."""
    existing = SavedOffer.objects.for_workspace(connection.workspace_id).filter(
        source=connection.source, item_id=item_id
    )
    if existing.exists():
        existing.delete()
        return False
    offer = find_offer(connection, item_id)
    SavedOffer.objects.get_or_create(
        workspace_id=connection.workspace_id,
        source=connection.source,
        item_id=item_id,
        defaults={"data": offer_to_dict(offer), "saved_by": user},
    )
    return True


def import_offer_image(connection: OpportunityConnection, offer: Offer, user):
    """Copy the offer's main image into the workspace media library.

    Raises ``ValueError`` when the offer has no usable image, the provider's
    errors when the CDN refuses, and the media library's quota errors.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.media_library.services import create_asset
    from apps.media_library.tasks import process_media_asset

    content, content_type = shopee_client(connection).download_image(offer.image_url)
    workspace = connection.workspace
    extension = {"image/png": "png", "image/webp": "webp"}.get(content_type, "jpg")
    asset = create_asset(
        workspace.organization,
        workspace,
        SimpleUploadedFile(f"shopee-{offer.item_id}.{extension}", content, content_type=content_type),
        user,
        title=offer.name[:255],
        alt_text=offer.name[:500],
        tags=["shopee"],
    )
    asset.source = "shopee"
    asset.source_url = offer.product_link[:200]
    asset.save(update_fields=["source", "source_url"])
    process_media_asset(str(asset.id))
    return asset


def create_offer_post(connection: OpportunityConnection, offer: Offer, caption: str, user, *, asset=None):
    """A draft post with the offer's text and image, ready to schedule in the composer.

    ``asset`` reuses an image already copied to the library; otherwise it is copied now.
    """
    from apps.composer.services import create_draft_post

    asset = asset or import_offer_image(connection, offer, user)
    return create_draft_post(
        workspace=connection.workspace,
        author=user,
        title=offer.name,
        caption=caption,
        media_asset_ids=[asset.id],
    )


def commission_label(offer: Offer) -> str:
    """ "Commission 15% · ~R$ 57,91", as the share dialog and the task brief show it."""
    return _("Commission %(pct)s%% · ~%(amount)s") % {
        "pct": floatformat(offer.commission_percent, -1),
        "amount": money(offer.commission),
    }


def offer_brief(offer: Offer) -> str:
    """The task description for an offer. The first line is the at-a-glance
    summary the task card shows; the links follow."""
    summary = " · ".join(
        part
        for part in (
            money(offer.price) if offer.price > 0 else "",
            _("%(pct)s%% OFF") % {"pct": offer.discount_rate} if offer.discount_rate else "",
            commission_label(offer),
        )
        if part
    )
    details = " · ".join(
        part
        for part in (
            offer.shop_name,
            f"★ {formats.number_format(offer.rating, 1, use_l10n=True)}" if offer.rating else "",
            ngettext("%(count)s sold", "%(count)s sold", offer.sales)
            % {"count": formats.number_format(offer.sales, 0, use_l10n=True, force_grouping=True)}
            if offer.sales
            else "",
        )
        if part
    )
    return "\n".join(
        line
        for line in (
            summary,
            details,
            _("Affiliate link: %(link)s") % {"link": offer.share_link},
            _("Product page: %(link)s") % {"link": offer.product_link} if offer.product_link else "",
        )
        if line
    )


def create_offer_task(
    connection: OpportunityConnection,
    offer: Offer,
    caption: str,
    user,
    *,
    art: dict | None = None,
    **fields,
):
    """A task to post the offer, from the new task window's ``fields``
    (``apps.tasks.views.parse_task_post``).

    With ``art`` (its formats, briefing and references) it is an art request
    (``apps.tasks.services.request_arts``); either way the product image is copied to the
    media library once and used twice: as a checklist item to work from, and in the
    draft post attached to the task, which goes out with ``caption``.
    """
    from apps.tasks.models import TaskChecklistItem
    from apps.tasks.services import create_task, request_arts

    asset = import_offer_image(connection, offer, user)
    post = create_offer_post(connection, offer, caption, user, asset=asset)
    fields["title"] = fields.get("title") or _("Post offer: %(name)s") % {"name": offer.name}
    fields["post_caption"] = caption
    image_item = {"text": _("Product image (copy in the media library)")[:255], "media_asset": asset}
    if art:
        task = request_arts(connection.workspace, user, **art, **fields)
        TaskChecklistItem.objects.create(task=task, position=task.checklist.count(), **image_item)
    else:
        task = create_task(connection.workspace, user, items=[image_item], **fields)
    task.post = post
    task.save(update_fields=["post", "updated_at"])
    return task


def describe_error(exc: ProviderError) -> str:
    """What to tell the user about a Shopee failure."""
    if isinstance(exc, OAuthError):
        return _("Shopee refused the App ID or Secret. Reconnect Shopee Affiliates in Platforms.")
    if isinstance(exc, RateLimitError):
        return _("Shopee is limiting requests right now. Wait a minute and try again.")
    if isinstance(exc, APIError) and exc.status_code == 403:
        return _(
            "This affiliate account has no access to the Shopee Open API. Check its status in the Shopee affiliate panel."
        )
    return _("Shopee could not answer right now. Try again in a moment.")


# ----------------------------------------------------------------------
# Sharing
# ----------------------------------------------------------------------


def money(value: Decimal) -> str:
    """BRL amount, rounded to cents (``number_format`` alone would truncate 57.905 to 57,90)."""
    rounded = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return "R$ " + formats.number_format(rounded, 2, use_l10n=True, force_grouping=True)


def share_texts(offer: Offer) -> dict[str, str]:
    """Ready-to-paste messages for one offer, keyed by template."""
    price = money(offer.price) if offer.price > 0 else ""
    link = offer.share_link
    stats = " · ".join(
        part
        for part in (
            f"⭐ {formats.number_format(offer.rating, 1, use_l10n=True)}" if offer.rating else "",
            _("🛒 %(count)s sold")
            % {"count": formats.number_format(offer.sales, 0, use_l10n=True, force_grouping=True)}
            if offer.sales
            else "",
        )
        if part
    )

    def join(*blocks: str) -> str:
        return "\n\n".join(b for b in blocks if b)

    return {
        "direct": join(
            _("🔥 SHOPEE DEAL!"),
            "\n".join(p for p in (offer.name, _("💰 For %(price)s") % {"price": price} if price else "", stats) if p),
            _("🛍️ Check it out: %(link)s") % {"link": link} if link else "",
            _("⚠️ Price and availability may change."),
        ),
        "urgent": join(
            _("🚨 LOOK AT THIS DEAL!"),
            "\n".join(p for p in (offer.name, _("🔥 Only %(price)s") % {"price": price} if price else "", stats) if p),
            _("Grab it while it lasts 👇\n%(link)s") % {"link": link} if link else "",
        ),
        "simple": join(
            "\n".join(p for p in (f"🛍️ {offer.name}", f"🔥 {price}" if price else "") if p),
            _("On Shopee 👇\n%(link)s") % {"link": link} if link else "",
        ),
    }


def search_terms(name: str) -> list[str]:
    """Short searches for videos of the product: its first 4, 3 and 2 words.

    Marketplace titles pile up keywords ("... Bordado com Lençol e Fronhas 200
    Fios"); the opening words are what people actually type on TikTok.
    """
    words = re.sub(r"[^\w\s]", " ", name.lower()).split()
    terms: list[str] = []
    for size in (4, 3, 2):
        term = " ".join(words[:size])
        if term and term not in terms:
            terms.append(term)
    return terms


def share_payload(offers: list[Offer]) -> dict[str, dict]:
    """What the share dialog needs per offer, for ``json_script``.

    Every visible string is formatted and translated here, so the dialog's
    JavaScript only places values.
    """
    payload = {}
    for o in offers:
        rating = formats.number_format(o.rating, 1, use_l10n=True) if o.rating else ""
        payload[str(o.item_id)] = {
            "name": o.name,
            "image": o.image_url,
            "link": o.share_link,
            "shop": o.shop_name,
            "price": money(o.price) if o.price > 0 else "",
            "discount": _("%(pct)s%% OFF") % {"pct": o.discount_rate} if o.discount_rate else "",
            "rating": rating,
            "sales": ngettext("%(count)s sold", "%(count)s sold", o.sales)
            % {"count": formats.number_format(o.sales, 0, use_l10n=True, force_grouping=True)}
            if o.sales
            else "",
            "commission": commission_label(o),
            "terms": search_terms(o.name),
            "texts": share_texts(o),
        }
    return payload
