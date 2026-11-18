"""Meta Marketing API client: ad accounts, Pixels, campaigns and their insights.

Not a social platform — nothing is published to a feed — so it is not in
``PROVIDER_REGISTRY``. ``apps.ads`` uses it to read and manage the campaigns of
the ad account a workspace linked.

It talks to the same Graph host and version as ``providers.facebook`` (bump
them together) and signs in through the same Meta app, asking for the ads
permissions on top of what the Login dialog would otherwise ask.

Graph answers a refused call with HTTP 4xx and ``{"error": {...}}``. The parts
worth showing are ``error_user_title`` / ``error_user_msg`` (written by Meta for
the advertiser, already in the user's language when the token's locale allows)
and ``message``. Codes ``190`` (invalid or expired token) and ``102`` (session)
mean the person has to sign in again; ``17``, ``4``, ``613`` and ``80004`` are
throttling.

Money: budgets and bids travel in the account currency's smallest unit (cents
for BRL), except for the currencies Meta treats as having no decimals.
Insights report ``spend`` as a decimal string in whole units.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx

from .base import HttpProvider
from .exceptions import APIError, ProviderError, RateLimitError, TokenExpiredError
from .facebook import BASE_URL, OAUTH_URL
from .meta_accounts import fetch_me_accounts
from .meta_oauth import facebook_login_params

logger = logging.getLogger(__name__)

# What the connection asks for. ``ads_management`` covers creating and editing;
# ``ads_read`` the insights; ``business_management`` the accounts and Pixels
# owned by a Business portfolio; ``pages_show_list`` + ``pages_manage_ads`` the
# Page an ad is published as.
ADS_SCOPES = [
    "ads_management",
    "ads_read",
    "business_management",
    "pages_show_list",
    "pages_manage_ads",
]

TOKEN_ERROR_CODES = {102, 190}
THROTTLE_ERROR_CODES = {4, 17, 32, 613, 80000, 80003, 80004, 80014}

# Currencies whose budgets Meta takes in whole units (offset 1); all others are
# in hundredths.
ZERO_DECIMAL_CURRENCIES = frozenset({"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"})

PAGE_SIZE = 100
# A guard against a cursor chain that never ends, not a limit anyone should reach.
MAX_PAGES = 10

ACCOUNT_FIELDS = "id,account_id,name,currency,timezone_name,account_status,business{id,name}"
PIXEL_FIELDS = "id,name,last_fired_time,is_unavailable"
CAMPAIGN_FIELDS = (
    "id,name,objective,status,effective_status,configured_status,daily_budget,lifetime_budget,"
    "start_time,stop_time,created_time,updated_time"
)
ADSET_FIELDS = (
    "id,name,status,effective_status,daily_budget,lifetime_budget,start_time,end_time,"
    "optimization_goal,billing_event,targeting,promoted_object"
)
AD_FIELDS = "id,name,status,effective_status,creative{id,name,thumbnail_url}"
INSIGHT_FIELDS = (
    "impressions,reach,frequency,clicks,inline_link_clicks,spend,ctr,cpc,cpm,actions,action_values,purchase_roas"
)
# ``actions`` entries counted as results, per what the campaign was set up for.
RESULT_ACTION_TYPES = {
    "purchases": ("offsite_conversion.fb_pixel_purchase", "purchase", "omni_purchase"),
    "leads": ("offsite_conversion.fb_pixel_lead", "lead", "onsite_conversion.lead_grouped"),
    "link_clicks": ("link_click",),
}

# ``account_status`` values. Only ACTIVE accounts can run ads.
ACCOUNT_ACTIVE = 1


@dataclass(frozen=True)
class AdAccount:
    id: str  # "act_<number>", the form every edge takes
    name: str
    currency: str
    timezone: str
    status: int
    business_name: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == ACCOUNT_ACTIVE


@dataclass(frozen=True)
class Pixel:
    id: str
    name: str
    last_fired_time: str = ""
    is_unavailable: bool = False


@dataclass(frozen=True)
class Page:
    id: str
    name: str
    picture: str = ""


@dataclass
class Insights:
    """One row of ``/insights``. Money in whole units of the account currency."""

    impressions: int = 0
    reach: int = 0
    frequency: Decimal = Decimal(0)
    clicks: int = 0
    link_clicks: int = 0
    spend: Decimal = Decimal(0)
    ctr: Decimal = Decimal(0)
    cpc: Decimal | None = None
    cpm: Decimal | None = None
    purchases: int = 0
    leads: int = 0
    purchase_value: Decimal = Decimal(0)
    roas: Decimal | None = None
    date_start: str = ""
    date_stop: str = ""
    campaign_id: str = ""
    campaign_name: str = ""
    extra: dict = field(default_factory=dict)


def to_minor_units(amount: Decimal, currency: str) -> int:
    """A budget in whole currency units → the integer Meta expects."""
    factor = 1 if currency.upper() in ZERO_DECIMAL_CURRENCIES else 100
    return int((amount * factor).to_integral_value())


def from_minor_units(value, currency: str) -> Decimal | None:
    """Meta's budget integer (a numeric string) → whole currency units. None when unset."""
    if value in (None, "", "0", 0):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    factor = 1 if currency.upper() in ZERO_DECIMAL_CURRENCIES else 100
    return number / factor


def _decimal(value, default: Decimal | None = Decimal(0)) -> Decimal | None:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return default


def _int(value) -> int:
    try:
        return int(Decimal(str(value or 0)))
    except InvalidOperation:
        return 0


def _sum_actions(entries, action_types: tuple[str, ...]) -> Decimal:
    """Total of the first listed action type present.

    Meta reports the same purchase under several names (pixel, omni, plain);
    adding them up would count it two or three times, so the most specific one
    reported wins.
    """
    by_type = {entry.get("action_type"): entry.get("value") for entry in entries or [] if isinstance(entry, dict)}
    for action_type in action_types:
        if action_type in by_type:
            return _decimal(by_type[action_type]) or Decimal(0)
    return Decimal(0)


def parse_insights_row(row: dict) -> Insights:
    actions = row.get("actions") or []
    values = row.get("action_values") or []
    roas_entries = row.get("purchase_roas") or []
    roas = _sum_actions(roas_entries, RESULT_ACTION_TYPES["purchases"]) if roas_entries else None
    return Insights(
        impressions=_int(row.get("impressions")),
        reach=_int(row.get("reach")),
        frequency=_decimal(row.get("frequency")) or Decimal(0),
        clicks=_int(row.get("clicks")),
        link_clicks=_int(row.get("inline_link_clicks")),
        spend=_decimal(row.get("spend")) or Decimal(0),
        ctr=_decimal(row.get("ctr")) or Decimal(0),
        cpc=_decimal(row.get("cpc"), None),
        cpm=_decimal(row.get("cpm"), None),
        purchases=int(_sum_actions(actions, RESULT_ACTION_TYPES["purchases"])),
        leads=int(_sum_actions(actions, RESULT_ACTION_TYPES["leads"])),
        purchase_value=_sum_actions(values, RESULT_ACTION_TYPES["purchases"]),
        roas=roas,
        date_start=row.get("date_start", ""),
        date_stop=row.get("date_stop", ""),
        campaign_id=row.get("campaign_id", ""),
        campaign_name=row.get("campaign_name", ""),
    )


def graph_error_message(body: dict) -> str:
    """The sentence Meta wrote for the advertiser, or its technical message."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    user_title = str(error.get("error_user_title") or "").strip()
    user_msg = str(error.get("error_user_msg") or "").strip()
    if user_msg:
        return f"{user_title}: {user_msg}" if user_title else user_msg
    return str(error.get("message") or "").strip()


class MetaAdsClient(HttpProvider):
    """Marketing API calls on behalf of one Meta user token."""

    def __init__(self, credentials: dict | None = None):
        creds = dict(credentials or {})
        if "app_id" in creds and "client_id" not in creds:
            creds["client_id"] = creds.pop("app_id")
        if "app_secret" in creds and "client_secret" not in creds:
            creds["client_secret"] = creds.pop("app_secret")
        self.credentials = creds

    @property
    def platform_name(self) -> str:
        return "Meta Ads"

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def _error_for_response(self, response: httpx.Response) -> ProviderError:
        """Keep Meta's own sentence and route token and throttle codes.

        The base message is the raw body cut at 500 characters, which is what
        a person would otherwise read under a failed campaign creation.
        """
        body = self._safe_json(response)
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        message = graph_error_message(body) or f"HTTP {response.status_code}"
        if code in TOKEN_ERROR_CODES:
            return TokenExpiredError(
                f"Meta Ads: {message}", status_code=response.status_code, platform=self.platform_name, raw_response=body
            )
        if code in THROTTLE_ERROR_CODES or response.status_code == 429:
            return RateLimitError(f"Meta Ads: {message}", platform=self.platform_name, raw_response=body)
        return APIError(
            f"Meta Ads: {message}",
            status_code=response.status_code,
            platform=self.platform_name,
            raw_response=body,
            retryable=response.status_code >= 500,
        )

    # ------------------------------------------------------------------
    # Sign-in
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str) -> str:
        params = facebook_login_params(
            client_id=self.credentials["client_id"],
            redirect_uri=redirect_uri,
            state=state,
            scopes=ADS_SCOPES,
            config_id=str(self.credentials.get("ads_config_id") or "").strip(),
        )
        return f"{OAUTH_URL}?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _get(self, token: str, path: str, params: dict | None = None) -> dict:
        return self._request("GET", f"{BASE_URL}/{path}", access_token=token, params=params).json()

    def _get_all(self, token: str, path: str, params: dict) -> list[dict]:
        """Every row of a paginated edge, up to ``MAX_PAGES`` pages."""
        rows: list[dict] = []
        params = {"limit": PAGE_SIZE, **params}
        for _page in range(MAX_PAGES):
            data = self._get(token, path, params)
            rows.extend(data.get("data") or [])
            paging = data.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not paging.get("next") or not after:
                return rows
            params = {**params, "after": after}
        logger.warning("Meta Ads %s truncated after %d rows", path, len(rows))
        return rows

    def ad_accounts(self, token: str) -> list[AdAccount]:
        rows = self._get_all(token, "me/adaccounts", {"fields": ACCOUNT_FIELDS})
        return [
            AdAccount(
                id=row["id"],
                name=row.get("name") or row.get("account_id") or row["id"],
                currency=row.get("currency") or "",
                timezone=row.get("timezone_name") or "",
                status=_int(row.get("account_status")),
                business_name=(row.get("business") or {}).get("name", ""),
            )
            for row in rows
            if row.get("id")
        ]

    def pixels(self, token: str, ad_account_id: str) -> list[Pixel]:
        rows = self._get_all(token, f"{ad_account_id}/adspixels", {"fields": PIXEL_FIELDS})
        return [
            Pixel(
                id=row["id"],
                name=row.get("name") or row["id"],
                last_fired_time=row.get("last_fired_time") or "",
                is_unavailable=bool(row.get("is_unavailable")),
            )
            for row in rows
            if row.get("id")
        ]

    def pages(self, token: str) -> list[Page]:
        raw = fetch_me_accounts(
            self,
            access_token=token,
            base_url=BASE_URL,
            fields="id,name,picture",
            error_message="Failed to fetch pages",
        )
        return [
            Page(
                id=row["id"],
                name=row.get("name", ""),
                picture=((row.get("picture") or {}).get("data") or {}).get("url", ""),
            )
            for row in raw
            if row.get("id")
        ]

    def campaigns(self, token: str, ad_account_id: str) -> list[dict]:
        return self._get_all(token, f"{ad_account_id}/campaigns", {"fields": CAMPAIGN_FIELDS})

    def campaign(self, token: str, campaign_id: str) -> dict:
        return self._get(token, campaign_id, {"fields": f"{CAMPAIGN_FIELDS},account_id"})

    def ad_sets(self, token: str, campaign_id: str) -> list[dict]:
        return self._get_all(token, f"{campaign_id}/adsets", {"fields": ADSET_FIELDS})

    def ads(self, token: str, campaign_id: str) -> list[dict]:
        return self._get_all(token, f"{campaign_id}/ads", {"fields": AD_FIELDS})

    def insights(
        self,
        token: str,
        object_id: str,
        *,
        since: str,
        until: str,
        level: str = "",
        daily: bool = False,
    ) -> list[Insights]:
        """``/insights`` of an account, campaign or ad set for ``since``..``until`` (YYYY-MM-DD).

        ``level="campaign"`` on an account gives one row per campaign;
        ``daily`` one row per day.
        """
        fields = INSIGHT_FIELDS
        params: dict = {"time_range": json.dumps({"since": since, "until": until})}
        if level:
            params["level"] = level
            if level == "campaign":
                fields = f"{fields},campaign_id,campaign_name"
        if daily:
            params["time_increment"] = 1
        params["fields"] = fields
        return [parse_insights_row(row) for row in self._get_all(token, f"{object_id}/insights", params)]

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _post(self, token: str, path: str, data: dict) -> dict:
        return self._request("POST", f"{BASE_URL}/{path}", access_token=token, data=data).json()

    def create_campaign(self, token: str, ad_account_id: str, *, name: str, objective: str) -> str:
        """A paused campaign whose budget lives on its ad sets."""
        data = self._post(
            token,
            f"{ad_account_id}/campaigns",
            {
                "name": name,
                "objective": objective,
                "status": "PAUSED",
                "special_ad_categories": "[]",
                # Required from v24 whenever the campaign carries no budget itself.
                "is_adset_budget_sharing_enabled": "false",
            },
        )
        return str(data["id"])

    def create_ad_set(self, token: str, ad_account_id: str, payload: dict) -> str:
        """Paused unless ``payload`` says otherwise."""
        data = self._post(token, f"{ad_account_id}/adsets", {"status": "PAUSED", **payload})
        return str(data["id"])

    def upload_image(self, token: str, ad_account_id: str, content: bytes) -> str:
        """Upload image bytes to the account's library and return their hash."""
        data = self._post(token, f"{ad_account_id}/adimages", {"bytes": base64.b64encode(content).decode("ascii")})
        images = data.get("images") or {}
        for image in images.values():
            if isinstance(image, dict) and image.get("hash"):
                return str(image["hash"])
        raise APIError("Meta Ads: the image upload returned no hash", platform=self.platform_name, raw_response=data)

    def create_creative(self, token: str, ad_account_id: str, *, name: str, object_story_spec: dict) -> str:
        data = self._post(
            token,
            f"{ad_account_id}/adcreatives",
            {"name": name, "object_story_spec": json.dumps(object_story_spec)},
        )
        return str(data["id"])

    def create_ad(
        self, token: str, ad_account_id: str, *, name: str, ad_set_id: str, creative_id: str, status: str = "PAUSED"
    ) -> str:
        data = self._post(
            token,
            f"{ad_account_id}/ads",
            {
                "name": name,
                "adset_id": ad_set_id,
                "creative": json.dumps({"creative_id": creative_id}),
                "status": status,
            },
        )
        return str(data["id"])

    def update(self, token: str, object_id: str, fields: dict) -> None:
        """Change fields of a campaign, ad set or ad (``POST /{id}``)."""
        data = self._post(token, object_id, fields)
        if data.get("success") is False:
            raise APIError("Meta Ads: the change was not accepted", platform=self.platform_name, raw_response=data)

    def delete(self, token: str, object_id: str) -> None:
        """Remove a campaign, ad set or ad Meta still holds (used to undo a half-made campaign)."""
        self._request("DELETE", f"{BASE_URL}/{object_id}", access_token=token)
