"""Pinterest Ads (API v5): ad accounts, campaigns, ad groups, ads and their analytics.

Not a social platform — nothing here publishes a pin — so it is not in
``PROVIDER_REGISTRY``. ``apps.ads.pinterest`` uses it for the ad account a
workspace linked. It signs in through the same Pinterest app as the publishing
accounts (``PinterestProvider``: same OAuth, same ``PINTEREST_API_BASE``, which
points at the Sandbox while the app has Trial access), asking for the ads
scopes instead of the pin ones.

Pinterest answers a refused call with HTTP 4xx and ``{"code": int, "message":
str}``; the message is kept as the person-facing sentence. Batch writes
(create, update) answer 200 with one item per object, each carrying either
``data`` or ``exceptions``.

Money: budgets and the ``*_IN_MICRO_DOLLAR`` analytics columns are in
millionths of the ad account's currency, whatever the currency is.
Analytics reach back at most 90 days and cover at most 90 days per call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from providers.meta_ads import Insights

from .exceptions import APIError, ProviderError, RateLimitError, TokenExpiredError
from .pinterest import API_BASE, PinterestProvider

logger = logging.getLogger(__name__)

# ``ads:read`` for the accounts, campaigns and analytics; ``ads:write`` to
# create and change them; ``user_accounts:read`` for who signed in.
ADS_SCOPES = ["user_accounts:read", "ads:read", "ads:write"]

MICRO = Decimal(1_000_000)
PAGE_SIZE = 100
# A guard against a bookmark chain that never ends, not a limit anyone should reach.
MAX_PAGES = 10
# How far back analytics go, counting today.
ANALYTICS_DAYS = 90
# campaign_ids per analytics call.
MAX_CAMPAIGN_IDS = 250

ANALYTICS_COLUMNS = (
    "SPEND_IN_MICRO_DOLLAR",
    "PAID_IMPRESSION",
    "TOTAL_IMPRESSION_USER",
    "CLICKTHROUGH_1",
    "OUTBOUND_CLICK_1",
    "TOTAL_CHECKOUT",
    "TOTAL_CHECKOUT_VALUE_IN_MICRO_DOLLAR",
    "TOTAL_LEAD",
)


@dataclass(frozen=True)
class AdAccount:
    id: str
    name: str
    currency: str
    country: str
    owner_username: str = ""


def from_micro(value) -> Decimal | None:
    """Pinterest's micro-currency integer → whole currency units. None when unset or 0."""
    if value in (None, "", 0, "0"):
        return None
    try:
        return Decimal(str(value)) / MICRO
    except InvalidOperation:
        return None


def to_micro(amount: Decimal) -> int:
    return int((amount * MICRO).to_integral_value())


def _int(value) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except InvalidOperation:
        return 0


def parse_analytics_row(row: dict) -> Insights:
    """One analytics row → the same ``Insights`` the Meta screens read.

    CTR, CPC and CPM are worked out here from the paid numbers, the way Meta
    reports them (CTR in %), so both platforms' cards mean the same thing.
    Columns without a value are left out of Pinterest's answer: they read as 0.
    """
    spend = from_micro(row.get("SPEND_IN_MICRO_DOLLAR")) or Decimal(0)
    impressions = _int(row.get("PAID_IMPRESSION"))
    reach = _int(row.get("TOTAL_IMPRESSION_USER"))
    clicks = _int(row.get("CLICKTHROUGH_1"))
    purchases = _int(row.get("TOTAL_CHECKOUT"))
    purchase_value = from_micro(row.get("TOTAL_CHECKOUT_VALUE_IN_MICRO_DOLLAR")) or Decimal(0)
    return Insights(
        impressions=impressions,
        reach=reach,
        frequency=Decimal(impressions) / reach if reach else Decimal(0),
        clicks=clicks,
        link_clicks=_int(row.get("OUTBOUND_CLICK_1")),
        spend=spend,
        ctr=Decimal(clicks) / impressions * 100 if impressions else Decimal(0),
        cpc=spend / clicks if clicks else None,
        cpm=spend / impressions * 1000 if impressions else None,
        purchases=purchases,
        leads=_int(row.get("TOTAL_LEAD")),
        purchase_value=purchase_value,
        roas=purchase_value / spend if spend and purchases else None,
        date_start=str(row.get("DATE") or ""),
        date_stop=str(row.get("DATE") or ""),
        campaign_id=str(row.get("CAMPAIGN_ID") or ""),
    )


class PinterestAdsClient(PinterestProvider):
    """Ads API calls on behalf of one Pinterest user token."""

    @property
    def platform_name(self) -> str:
        return "Pinterest Ads"

    @property
    def required_scopes(self) -> list[str]:
        return ADS_SCOPES

    def _error_for_response(self, response: httpx.Response) -> ProviderError:
        """Keep Pinterest's own sentence; 401 means signing in again, 429 waiting."""
        body = self._safe_json(response)
        message = str(body.get("message") or "") if isinstance(body, dict) else ""
        technical = f"Pinterest Ads: HTTP {response.status_code} {message}".strip()
        if response.status_code == 401:
            return TokenExpiredError(
                technical,
                status_code=401,
                platform=self.platform_name,
                raw_response=body,
                user_message=message,
            )
        if response.status_code == 429:
            return RateLimitError(technical, platform=self.platform_name, raw_response=body, user_message=message)
        return APIError(
            technical,
            status_code=response.status_code,
            platform=self.platform_name,
            raw_response=body,
            retryable=response.status_code >= 500,
            user_message=message,
        )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _get(self, token: str, path: str, params: dict | None = None):
        """GET ``path``; a list parameter goes as one comma-separated value.

        Pinterest reads only the last of a repeated parameter: sent as
        ``entity_statuses=ACTIVE&entity_statuses=PAUSED&entity_statuses=ARCHIVED``
        the campaigns list filtered on ARCHIVED alone and hid the paused ones.
        """
        query = {
            key: ",".join(map(str, value)) if isinstance(value, list | tuple) else value
            for key, value in (params or {}).items()
        }
        return self._request("GET", f"{API_BASE}/{path}", access_token=token, params=query).json()

    def _get_all(self, token: str, path: str, params: dict | None = None) -> list[dict]:
        """Every item of a bookmark-paged list, up to ``MAX_PAGES`` pages."""
        items: list[dict] = []
        params = {"page_size": PAGE_SIZE, **(params or {})}
        for _page in range(MAX_PAGES):
            body = self._get(token, path, params)
            items.extend(body.get("items") or [])
            bookmark = body.get("bookmark")
            if not bookmark:
                return items
            params = {**params, "bookmark": bookmark}
        logger.warning("Pinterest Ads %s truncated after %d items", path, len(items))
        return items

    def ad_accounts(self, token: str) -> list[AdAccount]:
        return [
            AdAccount(
                id=str(row.get("id")),
                name=row.get("name") or str(row.get("id")),
                currency=row.get("currency") or "",
                country=row.get("country") or "",
                owner_username=(row.get("owner") or {}).get("username") or "",
            )
            for row in self._get_all(token, "ad_accounts")
        ]

    def campaigns(self, token: str, ad_account_id: str) -> list[dict]:
        return self._get_all(
            token, f"ad_accounts/{ad_account_id}/campaigns", {"entity_statuses": ["ACTIVE", "PAUSED", "ARCHIVED"]}
        )

    def campaign(self, token: str, ad_account_id: str, campaign_id: str) -> dict:
        """One campaign, looked up inside the ad account: another account's id is a 404."""
        return self._get(token, f"ad_accounts/{ad_account_id}/campaigns/{campaign_id}")

    def ad_groups(self, token: str, ad_account_id: str, campaign_id: str) -> list[dict]:
        return self._get_all(
            token,
            f"ad_accounts/{ad_account_id}/ad_groups",
            {"campaign_ids": [campaign_id], "entity_statuses": ["ACTIVE", "PAUSED", "ARCHIVED"]},
        )

    def ads(self, token: str, ad_account_id: str, campaign_id: str) -> list[dict]:
        return self._get_all(
            token,
            f"ad_accounts/{ad_account_id}/ads",
            {"campaign_ids": [campaign_id], "entity_statuses": ["ACTIVE", "PAUSED", "ARCHIVED"]},
        )

    def audiences(self, token: str, ad_account_id: str) -> list[dict]:
        """The ad account's own audiences (customer lists, visitors, actalikes), for targeting."""
        return self._get_all(token, f"ad_accounts/{ad_account_id}/audiences")

    def conversion_tags(self, token: str, ad_account_id: str) -> list[dict]:
        """The ad account's Pinterest tags, which a Sales campaign optimises for."""
        body = self._get(token, f"ad_accounts/{ad_account_id}/conversion_tags")
        return body if isinstance(body, list) else (body.get("items") or [])

    def targeting_options(self, token: str, ad_account_id: str, targeting_type: str) -> list:
        """What Pinterest offers for one targeting type (``INTEREST``, ``GEO``…, in capitals), as it answers it."""
        body = self._get(token, f"resources/targeting/{targeting_type}", {"ad_account_id": ad_account_id})
        return body if isinstance(body, list) else (body.get("items") or [])

    def create_keywords(self, token: str, ad_account_id: str, ad_group_id: str, values: list[str]) -> None:
        """Broad-match keywords on an ad group; raise what Pinterest refused."""
        body = self._request(
            "POST",
            f"{API_BASE}/ad_accounts/{ad_account_id}/keywords",
            access_token=token,
            json={"parent_id": ad_group_id, "keywords": [{"match_type": "BROAD", "value": v} for v in values]},
        ).json()
        errors = (body.get("errors") or []) if isinstance(body, dict) else []
        if errors:
            message = str(errors[0].get("error_messages") or errors[0].get("message") or "")
            raise APIError(
                f"Pinterest Ads: keywords refused: {message or body}",
                status_code=400,
                platform=self.platform_name,
                raw_response=body,
                retryable=False,
                user_message=message,
            )

    def _analytics(self, token: str, path: str, params: dict, *, since: str, until: str, daily: bool) -> list[Insights]:
        rows = self._get(
            token,
            path,
            {
                **params,
                "start_date": since,
                "end_date": until,
                "columns": ",".join(ANALYTICS_COLUMNS),
                "granularity": "DAY" if daily else "TOTAL",
            },
        )
        return [parse_analytics_row(row) for row in rows or []]

    def account_analytics(
        self, token: str, ad_account_id: str, *, since: str, until: str, daily: bool = False
    ) -> list[Insights]:
        return self._analytics(
            token, f"ad_accounts/{ad_account_id}/analytics", {}, since=since, until=until, daily=daily
        )

    def campaign_analytics(
        self, token: str, ad_account_id: str, campaign_ids: list[str], *, since: str, until: str, daily: bool = False
    ) -> list[Insights]:
        """Rows per campaign (and per day when ``daily``); none when there are no campaigns."""
        rows: list[Insights] = []
        for start in range(0, len(campaign_ids), MAX_CAMPAIGN_IDS):
            rows += self._analytics(
                token,
                f"ad_accounts/{ad_account_id}/campaigns/analytics",
                {"campaign_ids": campaign_ids[start : start + MAX_CAMPAIGN_IDS]},
                since=since,
                until=until,
                daily=daily,
            )
        return rows

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _batch(self, method: str, token: str, path: str, item: dict) -> dict:
        """Send one object to a batch endpoint and return it, or raise what Pinterest refused."""
        body = self._request(method, f"{API_BASE}/{path}", access_token=token, json=[item]).json()
        results = body.get("items") or [] if isinstance(body, dict) else []
        result = results[0] if results else {}
        exceptions = result.get("exceptions") or []
        data = result.get("data")
        if exceptions or not data:
            message = str((exceptions[0] if exceptions else {}).get("message") or "")
            raise APIError(
                f"Pinterest Ads: {path} refused: {message or body}",
                status_code=400,
                platform=self.platform_name,
                raw_response=body if isinstance(body, dict) else {},
                retryable=False,
                user_message=message,
            )
        return data

    def create_ad_account(self, token: str, *, name: str, country: str, currency: str, owner_user_id: str) -> AdAccount:
        row = self._request(
            "POST",
            f"{API_BASE}/ad_accounts",
            access_token=token,
            json={"name": name, "country": country, "currency": currency, "owner_user_id": owner_user_id},
        ).json()
        return AdAccount(
            id=str(row.get("id")),
            name=row.get("name") or name,
            currency=row.get("currency") or currency,
            country=row.get("country") or country,
        )

    def create_campaign(self, token: str, ad_account_id: str, payload: dict) -> str:
        return str(self._batch("POST", token, f"ad_accounts/{ad_account_id}/campaigns", payload)["id"])

    def create_ad_group(self, token: str, ad_account_id: str, payload: dict) -> str:
        return str(self._batch("POST", token, f"ad_accounts/{ad_account_id}/ad_groups", payload)["id"])

    def create_ad(self, token: str, ad_account_id: str, payload: dict) -> str:
        return str(self._batch("POST", token, f"ad_accounts/{ad_account_id}/ads", payload)["id"])

    def update_campaign(self, token: str, ad_account_id: str, campaign_id: str, fields: dict) -> dict:
        return self._batch("PATCH", token, f"ad_accounts/{ad_account_id}/campaigns", {"id": campaign_id, **fields})
