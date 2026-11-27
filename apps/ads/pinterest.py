"""Pinterest Ads for one workspace: the connection, its campaigns and their numbers.

The Pinterest side of ``apps.ads``; the Meta side is ``apps.ads.services``,
whose ``Campaign``, comparison with the previous window and daily series are
reused here so both platforms' screens read the same objects.

Every call goes through the workspace's Pinterest ``AdsConnection`` token. A
campaign id from the browser is only ever read inside the chosen ad account
(``ad_accounts/<id>/campaigns/<campaign>``), so another account's campaign is
not found. The ad account itself is checked against the ones the token lists.

Campaigns made here are created with the campaign **paused** and its ad group
and ad active, so the campaign's own status is the one switch that spends, as
on Meta.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, pgettext_lazy

from apps.credentials.models import resolve_platform_credentials
from providers import pinterest_ads
from providers.exceptions import ProviderError, RateLimitError, TokenExpiredError
from providers.meta_ads import Insights

from . import services
from .models import AdsConnection
from .services import AdsError, AdsNotConfiguredError, Campaign

logger = logging.getLogger(__name__)

# The Pinterest app is the one the publishing accounts connect through.
CREDENTIAL_PLATFORM = "pinterest"
# Renew the access token this long before Pinterest's expiry.
REFRESH_MARGIN = timedelta(days=1)

OBJECTIVES = {
    "AWARENESS": gettext_lazy("Awareness"),
    "CONSIDERATION": gettext_lazy("Consideration"),
    "VIDEO_COMPLETION": gettext_lazy("Video views"),
    "VIDEO_VIEW": gettext_lazy("Video views"),
    "SALES": gettext_lazy("Sales"),
    "LEADS": gettext_lazy("Leads"),
    "WEB_CONVERSION": gettext_lazy("Conversions"),
    "CATALOG_SALES": gettext_lazy("Catalog sales"),
    "WEB_SESSIONS": gettext_lazy("Website visits"),
    "APP_INSTALL": gettext_lazy("App installs"),
    "ENGAGEMENT": gettext_lazy("Engagement"),
    "AWARENESS_RESERVED": gettext_lazy("Awareness"),
}
# What the creation offers, and what each ad group is billed on. Awareness,
# Consideration and Video completion are budgeted on the campaign (Pinterest's
# campaign budget optimization); Sales on its ad group, optimising for a
# Pinterest tag's checkouts.
CREATABLE_OBJECTIVES = ("AWARENESS", "CONSIDERATION", "VIDEO_COMPLETION", "SALES")
CBO_OBJECTIVES = ("AWARENESS", "CONSIDERATION", "VIDEO_COMPLETION")
BILLABLE_EVENTS = {
    "AWARENESS": "IMPRESSION",
    "CONSIDERATION": "CLICKTHROUGH",
    "VIDEO_COMPLETION": "IMPRESSION",
    "SALES": "IMPRESSION",
}

# Targeting values, as Pinterest names them (``targeting_spec``).
AGES = [str(age) for age in range(18, 66)]
GENDERS = {"female": gettext_lazy("Women"), "male": gettext_lazy("Men"), "unknown": gettext_lazy("Unspecified")}
# The languages Pinterest targets (ISO 639-1); the page names them in its own language.
LOCALES = (
    "pt", "en", "es", "fr", "de", "it", "nl", "sv", "da", "nb", "fi", "pl",
    "cs", "sk", "hu", "ro", "el", "tr", "ru", "ja", "ko", "zh", "id", "hi",
)  # fmt: skip
# The countries Pinterest delivers ads in (ISO 3166-1); named by the page too.
AD_COUNTRIES = (
    "AR", "AT", "AU", "BE", "BR", "CA", "CH", "CL", "CO", "CY", "CZ", "DE", "DK", "ES", "FI", "FR", "GB",
    "GR", "HU", "IE", "IT", "JP", "LU", "MT", "MX", "NL", "NO", "NZ", "PL", "PT", "RO", "SE", "SK", "US",
)  # fmt: skip
# Brazil's states as ISO 3166-2 region codes (``GEO``), to narrow a Brazilian audience.
BR_REGIONS = {
    "BR-AC": "Acre", "BR-AL": "Alagoas", "BR-AP": "Amapá", "BR-AM": "Amazonas", "BR-BA": "Bahia",
    "BR-CE": "Ceará", "BR-DF": "Distrito Federal", "BR-ES": "Espírito Santo", "BR-GO": "Goiás",
    "BR-MA": "Maranhão", "BR-MT": "Mato Grosso", "BR-MS": "Mato Grosso do Sul", "BR-MG": "Minas Gerais",
    "BR-PA": "Pará", "BR-PB": "Paraíba", "BR-PR": "Paraná", "BR-PE": "Pernambuco", "BR-PI": "Piauí",
    "BR-RJ": "Rio de Janeiro", "BR-RN": "Rio Grande do Norte", "BR-RS": "Rio Grande do Sul",
    "BR-RO": "Rondônia", "BR-RR": "Roraima", "BR-SC": "Santa Catarina", "BR-SP": "São Paulo",
    "BR-SE": "Sergipe", "BR-TO": "Tocantins",
}  # fmt: skip
PLACEMENTS = {
    "ALL": gettext_lazy("Feed and search"),
    "BROWSE": gettext_lazy("Home feed and related pins"),
    "SEARCH": gettext_lazy("Search results"),
}
DEVICES = {
    "iphone": "iPhone",
    "ipad": "iPad",
    "android_mobile": gettext_lazy("Android phone"),
    "android_tablet": gettext_lazy("Android tablet"),
    "web": gettext_lazy("Computer"),
    "web_mobile": gettext_lazy("Mobile browser"),
}
BID_STRATEGIES = {
    "AUTOMATIC_BID": gettext_lazy("Automatic"),
    "MAX_BID": gettext_lazy("Maximum bid"),
    "TARGET_AVG": gettext_lazy("Target average cost"),
}
# Keywords only show where people search, so they need a placement that includes search.
KEYWORD_PLACEMENTS = ("ALL", "SEARCH")
MAX_KEYWORDS = 200
# Pinterest's interest list barely changes; read once a day per ad account.
INTEREST_CACHE_SECONDS = 24 * 3600

# ``summary_status`` (what is happening) → (label, pill tone).
SUMMARY_STATUSES = {
    "RUNNING": (gettext_lazy("Active"), "success"),
    "ACTIVE": (gettext_lazy("Active"), "success"),
    "PAUSED": (gettext_lazy("Paused"), "neutral"),
    "NOT_STARTED": (gettext_lazy("Scheduled"), "info"),
    "COMPLETED": (gettext_lazy("Completed"), "neutral"),
    "ADVERTISER_DISABLED": (gettext_lazy("Disabled"), "warning"),
    "ARCHIVED": (pgettext_lazy("ad status", "Archived"), "neutral"),
    "DRAFT": (gettext_lazy("Draft"), "neutral"),
    "PENDING": (gettext_lazy("In review"), "info"),
    "APPROVED": (gettext_lazy("Approved"), "success"),
    "REJECTED": (gettext_lazy("Rejected"), "error"),
}

# What a new ad account can be opened in.
COUNTRIES = {"BR": gettext_lazy("Brazil"), "PT": gettext_lazy("Portugal"), "US": gettext_lazy("United States")}
CURRENCIES = ("BRL", "EUR", "USD")


# ------------------------------------------------------------------
# Credentials, tokens and errors
# ------------------------------------------------------------------


def is_offered() -> bool:
    """Whether Pinterest Ads may be connected: it follows Pinterest's switch in
    the administrative area (``PlatformVisibility``), since both sign in through
    the same Pinterest app. A workspace already connected keeps its connection.
    """
    from apps.social_accounts.models import PlatformVisibility

    return CREDENTIAL_PLATFORM not in PlatformVisibility.hidden_keys()


def app_credentials(org_id) -> dict:
    creds = resolve_platform_credentials(CREDENTIAL_PLATFORM, org_id)
    client_id = str(creds.get("client_id") or creds.get("app_id") or "").strip()
    client_secret = str(creds.get("client_secret") or creds.get("app_secret") or "").strip()
    if not client_id or not client_secret:
        raise AdsNotConfiguredError(_("The Pinterest app is not configured. Ask your administrator to set it up."))
    return {"client_id": client_id, "client_secret": client_secret}


def is_configured(org_id) -> bool:
    try:
        app_credentials(org_id)
    except AdsNotConfiguredError:
        return False
    return True


def client_for_org(org_id) -> pinterest_ads.PinterestAdsClient:
    return pinterest_ads.PinterestAdsClient(app_credentials(org_id))


def describe_error(exc: ProviderError) -> str:
    if isinstance(exc, TokenExpiredError):
        return _("The Pinterest sign-in expired or was revoked. Connect Pinterest Ads again.")
    if isinstance(exc, RateLimitError):
        return _("Pinterest is limiting requests to this ad account right now. Try again in a few minutes.")
    detail = exc.user_message or str(exc).removeprefix("Pinterest Ads: ").strip()
    return _("Pinterest refused the request: %(detail)s") % {"detail": detail or _("no reason given")}


def _expires_at(tokens) -> datetime | None:
    return timezone.now() + timedelta(seconds=tokens.expires_in) if tokens.expires_in else None


def _client(connection: AdsConnection) -> pinterest_ads.PinterestAdsClient:
    """Kept on the connection, so the several calls of one page resolve the app once."""
    client = getattr(connection, "_ads_client", None)
    if client is None:
        client = client_for_org(connection.workspace.organization_id)
        connection._ads_client = client  # type: ignore[attr-defined]
    return client


def _refresh(connection: AdsConnection) -> bool:
    """Trade the refresh token for a new access token; False when there is none or Pinterest refuses."""
    if not connection.refresh_token:
        return False
    try:
        tokens = _client(connection).refresh_token(connection.refresh_token)
    except ProviderError as exc:
        logger.warning("Pinterest Ads token refresh failed for workspace %s: %s", connection.workspace_id, exc)
        return False
    connection.access_token = tokens.access_token
    connection.refresh_token = tokens.refresh_token or connection.refresh_token
    connection.token_expires_at = _expires_at(tokens)
    connection.save(update_fields=["access_token", "refresh_token", "token_expires_at", "updated_at"])
    return True


def _record_failure(connection: AdsConnection, exc: ProviderError) -> None:
    if isinstance(exc, TokenExpiredError):
        connection.status = AdsConnection.Status.ERROR
        connection.last_error = describe_error(exc)
        connection.save(update_fields=["status", "last_error", "updated_at"])


def _call(connection: AdsConnection, fn, *args, **kwargs):
    """Run a client call with the connection's token, renewed first when it is about to lapse.

    A 401 gets one renewal and one retry; Pinterest's refusal becomes an ``AdsError``.
    """
    client = _client(connection)
    expires = connection.token_expires_at
    if expires and expires - timezone.now() <= REFRESH_MARGIN:
        _refresh(connection)
    try:
        try:
            return fn(client, connection.access_token, *args, **kwargs)
        except TokenExpiredError:
            if not _refresh(connection):
                raise
            return fn(client, connection.access_token, *args, **kwargs)
    except ProviderError as exc:
        logger.warning("Pinterest Ads call %s failed for workspace %s: %s", fn.__name__, connection.workspace_id, exc)
        _record_failure(connection, exc)
        raise AdsError(describe_error(exc)) from exc


# ------------------------------------------------------------------
# Connection
# ------------------------------------------------------------------


def get_connection(workspace_id) -> AdsConnection | None:
    return (
        AdsConnection.objects.for_workspace(workspace_id)
        .filter(platform=AdsConnection.Platform.PINTEREST)
        .select_related("workspace")
        .first()
    )


def finish_sign_in(*, workspace, user, code: str, redirect_uri: str) -> AdsConnection:
    """Trade the code for tokens and keep them on the workspace.

    A reconnect keeps the ad account already chosen while the new token still
    reaches it; otherwise the choice starts over.
    """
    client = pinterest_ads.PinterestAdsClient(app_credentials(workspace.organization_id))
    try:
        tokens = client.exchange_code(code, redirect_uri)
        profile = client.get_profile(tokens.access_token)
        reachable = {account.id for account in client.ad_accounts(tokens.access_token)}
    except ProviderError as exc:
        logger.warning("Pinterest Ads sign-in failed for workspace %s: %s", workspace.id, exc)
        raise AdsError(describe_error(exc)) from exc

    with transaction.atomic():
        connection = (
            AdsConnection.objects.select_for_update()
            .filter(workspace=workspace, platform=AdsConnection.Platform.PINTEREST)
            .first()
        )
        if connection is None:
            connection = AdsConnection(workspace=workspace, platform=AdsConnection.Platform.PINTEREST)
        connection.access_token = tokens.access_token
        connection.refresh_token = tokens.refresh_token or ""
        connection.token_expires_at = _expires_at(tokens)
        connection.meta_user_id = profile.platform_id
        connection.meta_user_name = profile.handle or profile.name
        connection.connected_by = user
        connection.last_error = ""
        if connection.ad_account_id and connection.ad_account_id in reachable:
            connection.status = AdsConnection.Status.CONNECTED
        else:
            connection.ad_account_id = connection.ad_account_name = connection.currency = ""
            connection.status = AdsConnection.Status.PENDING
        connection.save()
    return connection


def ad_accounts(connection: AdsConnection) -> list[pinterest_ads.AdAccount]:
    return _call(connection, pinterest_ads.PinterestAdsClient.ad_accounts)


def choose_account(connection: AdsConnection, ad_account_id: str) -> AdsConnection:
    """Keep the ad account, only if this token really reaches it."""
    account = next((a for a in ad_accounts(connection) if a.id == ad_account_id), None)
    if account is None:
        raise AdsError(_("Choose one of the ad accounts your Pinterest sign-in can manage."))
    _keep_account(connection, account)
    return connection


def _keep_account(connection: AdsConnection, account: pinterest_ads.AdAccount) -> None:
    connection.ad_account_id = account.id
    connection.ad_account_name = account.name
    connection.currency = account.currency
    connection.status = AdsConnection.Status.CONNECTED
    connection.last_error = ""
    connection.save()


def create_ad_account(connection: AdsConnection, *, name: str, country: str, currency: str) -> AdsConnection:
    """Open an ad account owned by the Pinterest user who signed in, and choose it."""
    if country not in COUNTRIES or currency not in CURRENCIES:
        raise AdsError(_("Choose one of the offered countries and currencies."))
    account = _call(
        connection,
        pinterest_ads.PinterestAdsClient.create_ad_account,
        name=name,
        country=country,
        currency=currency,
        owner_user_id=connection.meta_user_id,
    )
    _keep_account(connection, account)
    return connection


def disconnect(connection: AdsConnection) -> None:
    """Forget the tokens. Campaigns keep running on Pinterest: they belong to the ad account."""
    connection.delete()


# ------------------------------------------------------------------
# Campaigns
# ------------------------------------------------------------------


def _status(value: str) -> tuple[str, str]:
    label, tone = SUMMARY_STATUSES.get(value, (value.replace("_", " ").capitalize(), "neutral"))
    return str(label), tone


def _from_unix(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=UTC) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def _manager_url(connection: AdsConnection) -> str:
    return f"https://ads.pinterest.com/advertiser/{connection.ad_account_id}/"


def _campaign_from_row(connection: AdsConnection, row: dict) -> Campaign:
    objective = row.get("objective_type") or ""
    label, tone = _status(row.get("summary_status") or row.get("status") or "")
    return Campaign(
        id=str(row.get("id")),
        name=row.get("name") or "",
        objective=objective,
        objective_label=str(OBJECTIVES.get(objective, objective.replace("_", " ").capitalize())),
        status=row.get("status") or "",
        effective_status=row.get("summary_status") or "",
        status_label=label,
        status_tone=tone,
        daily_budget=pinterest_ads.from_micro(row.get("daily_spend_cap")),
        lifetime_budget=pinterest_ads.from_micro(row.get("lifetime_spend_cap")),
        start_time=_from_unix(row.get("start_time")),
        stop_time=_from_unix(row.get("end_time")),
        manager_url=_manager_url(connection),
    )


def earliest_day() -> date:
    """The first day Pinterest still has numbers for."""
    return timezone.now().astimezone(UTC).date() - timedelta(days=pinterest_ads.ANALYTICS_DAYS - 1)


def _windows(days: int, end: date | None) -> tuple[tuple[str, str], tuple[str, str] | None]:
    """The window asked for and the one before it, or None for the latter when
    Pinterest no longer has it. A window Pinterest does not have is an ``AdsError``."""
    current = services.date_window(days, end)
    if date.fromisoformat(current[0]) < earliest_day():
        raise AdsError(_("Pinterest keeps the numbers of the last 90 days. Choose a period inside them."))
    previous = services.previous_window(days, end)
    return current, (previous if date.fromisoformat(previous[0]) >= earliest_day() else None)


def _per_campaign(connection: AdsConnection, ids: list[str], window: tuple[str, str]) -> dict[str, Insights]:
    if not ids:
        return {}
    rows = _call(
        connection,
        pinterest_ads.PinterestAdsClient.campaign_analytics,
        connection.ad_account_id,
        ids,
        since=window[0],
        until=window[1],
    )
    return {row.campaign_id: row for row in rows}


def list_campaigns(
    connection: AdsConnection, *, days: int = services.DEFAULT_RANGE, end: date | None = None
) -> list[Campaign]:
    """The ad account's campaigns (archived ones last), each with its numbers for the
    window and, when Pinterest still has it, for the window before."""
    current, previous = _windows(days, end)
    rows = _call(connection, pinterest_ads.PinterestAdsClient.campaigns, connection.ad_account_id)
    campaigns = [_campaign_from_row(connection, row) for row in rows]
    ids = [c.id for c in campaigns]
    now = _per_campaign(connection, ids, current)
    before = _per_campaign(connection, ids, previous) if previous else {}
    for campaign in campaigns:
        campaign.insights = now.get(campaign.id, Insights())
        campaign.previous = before.get(campaign.id, Insights())
        campaign.compared = previous is not None
    campaigns.sort(key=lambda c: (c.status == "ARCHIVED", c.status != "ACTIVE"))
    return campaigns


def get_campaign(connection: AdsConnection, campaign_id: str) -> Campaign:
    """One campaign of *this* ad account, or ``AdsError``."""
    if not campaign_id.isdigit():
        raise AdsError(_("This campaign was not found in the connected ad account."))
    row = _call(connection, pinterest_ads.PinterestAdsClient.campaign, connection.ad_account_id, campaign_id)
    if str(row.get("ad_account_id") or connection.ad_account_id) != connection.ad_account_id:
        raise AdsError(_("This campaign was not found in the connected ad account."))
    return _campaign_from_row(connection, row)


@dataclass
class AdGroup:
    id: str
    name: str
    status_label: str
    status_tone: str
    budget: Decimal | None
    budget_type: str
    countries: list[str] = field(default_factory=list)


def _first(rows: list[Insights]) -> Insights:
    return rows[0] if rows else Insights()


def _account_rows(connection: AdsConnection, window: tuple[str, str], *, daily: bool = False) -> list[Insights]:
    return _call(
        connection,
        pinterest_ads.PinterestAdsClient.account_analytics,
        connection.ad_account_id,
        since=window[0],
        until=window[1],
        daily=daily,
    )


def account_overview(connection: AdsConnection, *, days: int = services.DEFAULT_RANGE, end: date | None = None) -> dict:
    """The ad account's totals and daily series for the window, and how each total
    moved against the window before it when Pinterest still has it."""
    current, previous = _windows(days, end)
    totals = _first(_account_rows(connection, current))
    changes = services.compare_all(totals, _first(_account_rows(connection, previous))) if previous else {}
    daily = _account_rows(connection, current, daily=True)
    return {"totals": totals, "changes": changes, "series": services.daily_series(daily, *current)}


def campaign_detail(
    connection: AdsConnection, campaign_id: str, *, days: int = services.DEFAULT_RANGE, end: date | None = None
) -> dict:
    """A campaign's numbers, its ad groups and its ads."""
    current, previous = _windows(days, end)
    campaign = get_campaign(connection, campaign_id)

    def rows(window, daily=False):
        return _call(
            connection,
            pinterest_ads.PinterestAdsClient.campaign_analytics,
            connection.ad_account_id,
            [campaign.id],
            since=window[0],
            until=window[1],
            daily=daily,
        )

    totals = _first(rows(current))
    before = _first(rows(previous)) if previous else Insights()
    campaign.insights, campaign.previous, campaign.compared = totals, before, previous is not None

    ad_groups = []
    for row in _call(connection, pinterest_ads.PinterestAdsClient.ad_groups, connection.ad_account_id, campaign.id):
        label, tone = _status(row.get("summary_status") or row.get("status") or "")
        ad_groups.append(
            AdGroup(
                id=str(row.get("id")),
                name=row.get("name") or "",
                status_label=label,
                status_tone=tone,
                budget=pinterest_ads.from_micro(row.get("budget_in_micro_currency")),
                budget_type=row.get("budget_type") or "",
                countries=list((row.get("targeting_spec") or {}).get("GEO") or []),
            )
        )
    ads = []
    for row in _call(connection, pinterest_ads.PinterestAdsClient.ads, connection.ad_account_id, campaign.id):
        review = row.get("review_status") or ""
        # A rejected or pending review says more than "active".
        label, tone = _status(review if review in ("PENDING", "REJECTED") else row.get("summary_status") or "")
        pin_id = str(row.get("pin_id") or "")
        ads.append(
            {
                "id": row.get("id"),
                "name": row.get("name") or pin_id,
                "status_label": label,
                "status_tone": tone,
                "pin_id": pin_id,
                "pin_url": f"https://www.pinterest.com/pin/{pin_id}/" if pin_id.isdigit() else "",
            }
        )
    return {
        "campaign": campaign,
        "ad_groups": ad_groups,
        "ads": ads,
        "totals": totals,
        "changes": services.compare_all(totals, before) if previous else {},
        "series": services.daily_series(rows(current, daily=True), *current),
    }


def set_campaign_status(connection: AdsConnection, campaign_id: str, status: str) -> Campaign:
    """Start (``ACTIVE``) or stop (``PAUSED``) a campaign; ``ARCHIVED`` retires it for good."""
    if status not in ("ACTIVE", "PAUSED", "ARCHIVED"):
        raise AdsError(_("Unknown campaign status."))
    campaign = get_campaign(connection, campaign_id)
    _call(
        connection,
        pinterest_ads.PinterestAdsClient.update_campaign,
        connection.ad_account_id,
        campaign.id,
        {"status": status},
    )
    campaign.status = status
    return campaign


def update_campaign(
    connection: AdsConnection,
    campaign_id: str,
    *,
    name: str,
    daily_budget: Decimal | None,
    end_time: datetime | None,
) -> None:
    """Rename a campaign and change its daily budget and end. The budget is the
    campaign's: the simplified creation budgets on the campaign."""
    campaign = get_campaign(connection, campaign_id)
    fields: dict = {"name": name}
    if daily_budget is not None:
        if campaign.lifetime_budget:
            raise AdsError(_("This campaign has a lifetime budget. Change it in Pinterest Ads Manager."))
        fields["daily_spend_cap"] = pinterest_ads.to_micro(daily_budget)
    if end_time is not None:
        fields["end_time"] = int(end_time.timestamp())
    _call(connection, pinterest_ads.PinterestAdsClient.update_campaign, connection.ad_account_id, campaign.id, fields)


# ------------------------------------------------------------------
# Creation
# ------------------------------------------------------------------


@dataclass
class NewCampaign:
    name: str
    objective: str
    daily_budget: Decimal
    start_time: datetime | None
    end_time: datetime | None
    countries: list[str]
    pin_id: str
    is_video: bool = False
    regions: list[str] = field(default_factory=list)
    age_min: str = ""
    age_max: str = ""
    genders: list[str] = field(default_factory=list)
    locales: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    audiences_include: list[str] = field(default_factory=list)
    audiences_exclude: list[str] = field(default_factory=list)
    auto_targeting: bool = True
    placement: str = "ALL"
    devices: list[str] = field(default_factory=list)
    bid_strategy: str = "AUTOMATIC_BID"
    bid_amount: Decimal | None = None
    frequency_cap: int | None = None
    conversion_tag_id: str = ""


def build_campaign_payload(new: NewCampaign) -> dict:
    """The campaign, paused. Budget-optimised objectives carry the daily budget here."""
    payload: dict = {"name": new.name, "objective_type": new.objective, "status": "PAUSED"}
    if new.objective in CBO_OBJECTIVES:
        payload["daily_spend_cap"] = pinterest_ads.to_micro(new.daily_budget)
    if new.start_time:
        payload["start_time"] = int(new.start_time.timestamp())
    if new.end_time:
        payload["end_time"] = int(new.end_time.timestamp())
    return payload


def build_targeting_spec(new: NewCampaign) -> dict:
    """``targeting_spec`` from the chosen audience; an empty choice is left out, which
    Pinterest reads as "everyone" for that dimension."""
    spec: dict = {"GEO": new.regions or new.countries}
    if new.age_min or new.age_max:
        spec["MINIMUM_AGE"] = new.age_min or "18"
        spec["MAXIMUM_AGE"] = new.age_max or "65+"
    optional = {
        "GENDER": new.genders,
        "LOCALE": new.locales,
        "INTEREST": new.interests,
        "AUDIENCE_INCLUDE": new.audiences_include,
        "AUDIENCE_EXCLUDE": new.audiences_exclude,
        "APPTYPE": new.devices,
    }
    spec.update({key: values for key, values in optional.items() if values})
    return spec


def build_ad_group_payload(new: NewCampaign, campaign_id: str) -> dict:
    """The campaign's one ad group: its audience, placement and bid, billed per the objective.

    A budget-optimised campaign carries the budget; a Sales one puts it here and
    optimises for the chosen Pinterest tag's checkouts.
    """
    payload: dict = {
        "name": f"{new.name} — {_('Ad group')}",
        "campaign_id": campaign_id,
        "status": "ACTIVE",
        "billable_event": BILLABLE_EVENTS[new.objective],
        "bid_strategy_type": new.bid_strategy,
        "targeting_spec": build_targeting_spec(new),
        "placement_group": new.placement,
        "auto_targeting_enabled": new.auto_targeting,
    }
    if new.bid_strategy != "AUTOMATIC_BID" and new.bid_amount:
        payload["bid_in_micro_currency"] = pinterest_ads.to_micro(new.bid_amount)
    if new.objective not in CBO_OBJECTIVES:
        payload["budget_in_micro_currency"] = pinterest_ads.to_micro(new.daily_budget)
        payload["budget_type"] = "DAILY"
    if new.objective == "SALES":
        payload["optimization_goal_metadata"] = {
            "conversion_tag_v3_goal_metadata": {
                "conversion_tag_id": new.conversion_tag_id,
                "conversion_event": "CHECKOUT",
                "attribution_windows": {"click_window_days": 30, "engagement_window_days": 30, "view_window_days": 1},
            }
        }
    if new.frequency_cap and new.objective == "AWARENESS" and new.end_time:
        payload["lifetime_frequency_cap"] = new.frequency_cap
    if new.start_time:
        payload["start_time"] = int(new.start_time.timestamp())
    if new.end_time:
        payload["end_time"] = int(new.end_time.timestamp())
    return payload


def build_ad_payload(new: NewCampaign, ad_group_id: str) -> dict:
    return {
        "ad_group_id": ad_group_id,
        "pin_id": new.pin_id,
        "creative_type": "VIDEO" if new.is_video else "REGULAR",
        "status": "ACTIVE",
        "name": new.name,
    }


def create_campaign(connection: AdsConnection, new: NewCampaign) -> str:
    """Create the campaign, its ad group (with its keywords) and the ad that promotes
    the pin; return the campaign id.

    The campaign is created paused. Pinterest has no deleting a campaign, so a
    failure part-way archives it, which also stops its ad group and ad.
    """
    if not connection.is_ready:
        raise AdsError(_("Finish connecting Pinterest Ads before creating campaigns."))
    if new.objective not in CREATABLE_OBJECTIVES:
        raise AdsError(_("Choose one of the offered objectives."))
    if not new.pin_id.isdigit():
        raise AdsError(_("Choose the pin the ad promotes."))
    account = connection.ad_account_id
    client = pinterest_ads.PinterestAdsClient
    campaign_id = _call(connection, client.create_campaign, account, build_campaign_payload(new))
    try:
        ad_group_id = _call(connection, client.create_ad_group, account, build_ad_group_payload(new, campaign_id))
        if new.keywords and new.placement in KEYWORD_PLACEMENTS:
            _call(connection, client.create_keywords, account, ad_group_id, new.keywords)
        _call(connection, client.create_ad, account, build_ad_payload(new, ad_group_id))
    except AdsError:
        try:
            _call(connection, client.update_campaign, account, campaign_id, {"status": "ARCHIVED"})
        except AdsError:
            logger.error("Pinterest Ads campaign %s was left half-created and could not be archived", campaign_id)
        raise
    return campaign_id


# ------------------------------------------------------------------
# What the creation form offers from the ad account
# ------------------------------------------------------------------


def _option(row) -> dict | None:
    """One targeting option as ``{"id", "name"}``; Pinterest answers either an object
    with ``id``/``name`` or a one-entry ``{id: name}`` map."""
    if not isinstance(row, dict):
        return None
    if "id" in row:
        return {"id": str(row["id"]), "name": str(row.get("name") or row["id"])}
    if len(row) == 1:
        key, value = next(iter(row.items()))
        return {"id": str(key), "name": str(value)}
    return None


def _flatten_interests(rows) -> list[dict]:
    """Interests with their sub-interests, each once, sorted by name."""
    found: dict[str, dict] = {}
    stack = list(rows or [])
    while stack:
        row = stack.pop()
        option = _option(row)
        if option:
            found.setdefault(option["id"], option)
        if isinstance(row, dict):
            stack.extend(row.get("child_interests") or row.get("children") or [])
    return sorted(found.values(), key=lambda o: o["name"].lower())


def targeting_choices(connection: AdsConnection) -> dict:
    """What the ad account offers the creation form: interests, its audiences and its
    Pinterest tags. Each is read on its own; one Pinterest refuses comes back as its
    reason in ``errors`` and the form goes on without it."""
    from django.core.cache import cache

    client = pinterest_ads.PinterestAdsClient
    account = connection.ad_account_id
    choices: dict = {"interests": [], "audiences": [], "conversion_tags": [], "errors": {}}

    cache_key = f"pinterest-ads:interests:{account}"
    interests = cache.get(cache_key)
    if interests is None:
        try:
            interests = _flatten_interests(_call(connection, client.targeting_options, account, "interest"))
        except AdsError as exc:
            choices["errors"]["interests"] = str(exc)
            interests = []
        else:
            cache.set(cache_key, interests, INTEREST_CACHE_SECONDS)
    choices["interests"] = interests

    try:
        rows = _call(connection, client.audiences, account)
    except AdsError as exc:
        choices["errors"]["audiences"] = str(exc)
    else:
        choices["audiences"] = [
            {"id": str(row.get("id")), "name": row.get("name") or str(row.get("id"))}
            for row in rows
            if row.get("id") and row.get("status") in (None, "READY")
        ]

    try:
        rows = _call(connection, client.conversion_tags, account)
    except AdsError as exc:
        choices["errors"]["conversion_tags"] = str(exc)
    else:
        choices["conversion_tags"] = [
            {"id": str(row.get("id")), "name": row.get("name") or str(row.get("id"))}
            for row in rows
            if row.get("id") and row.get("status") in (None, "ACTIVE")
        ]
    return choices


def _preview_image(post) -> str:
    """The picture a post shows first: its first media's thumbnail, or an image's own file."""
    attachments = post.media_attachments.all()
    if not attachments:
        return ""
    asset = attachments[0].media_asset
    if asset.thumbnail:
        return asset.thumbnail.url
    if asset.media_type == asset.MediaType.IMAGE and asset.file:
        return asset.file.url
    return ""


def promotable_pins(workspace, limit: int = 48) -> list[dict]:
    """The workspace's published pins, newest first, for the ad's picker.

    ``is_video`` decides the ad's creative type, which Pinterest wants told;
    ``post`` carries the media for the picker's thumbnail, ``image_url`` the
    picture the ad preview shows (the first media's thumbnail, or the image
    itself), ``avatar_url`` the account's picture.
    """
    from apps.composer.models import PlatformPost
    from apps.composer.services import media_types_by_post
    from apps.media_library.models import MediaAsset

    rows = (
        PlatformPost.objects.filter(
            post__workspace=workspace,
            social_account__workspace=workspace,
            social_account__platform="pinterest",
            status=PlatformPost.Status.PUBLISHED,
        )
        .exclude(platform_post_id="")
        .exclude(platform_post_id__isnull=True)
        .select_related("post", "social_account")
        .prefetch_related("post__media_attachments__media_asset")
        .order_by("-published_at")[:limit]
    )
    media = media_types_by_post([row.post_id for row in rows])
    pins = []
    for row in rows:
        if not str(row.platform_post_id).isdigit():
            continue
        pins.append(
            {
                "id": str(row.platform_post_id),
                "post": row.post,
                "title": row.platform_specific_title or row.post.title or "",
                "caption": row.platform_specific_caption or row.post.caption or "",
                "account": row.social_account.account_name,
                "published_at": row.published_at,
                "is_video": MediaAsset.MediaType.VIDEO in media.get(row.post_id, set()),
                "image_url": _preview_image(row.post),
                "avatar_url": row.social_account.avatar_url,
            }
        )
    return pins
