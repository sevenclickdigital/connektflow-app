"""Meta Ads for one workspace: the connection, its campaigns and their numbers.

Every call goes through the workspace's ``AdsConnection`` token, and every id
that arrives from the browser — ad account, Pixel, Page, campaign, ad set — is
checked against what Meta lists for that token and that ad account before it
is used. A Meta user can reach several ad accounts; the workspace only ever
touches the one it chose.

Campaigns made here are created with the campaign **paused** and its ad set and
ad active, so the campaign's own status is the one switch that starts and stops
spending.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.credentials.models import resolve_platform_credentials
from providers import meta_ads
from providers.exceptions import ProviderError, RateLimitError, TokenExpiredError
from providers.facebook import FacebookProvider

from .models import AdsConnection

logger = logging.getLogger(__name__)

# The Meta app is the one Facebook Pages connect through.
CREDENTIAL_PLATFORM = "facebook"

OBJECTIVES = {
    "OUTCOME_AWARENESS": gettext_lazy("Awareness"),
    "OUTCOME_TRAFFIC": gettext_lazy("Traffic"),
    "OUTCOME_ENGAGEMENT": gettext_lazy("Engagement"),
    "OUTCOME_LEADS": gettext_lazy("Leads"),
    "OUTCOME_SALES": gettext_lazy("Sales"),
    "OUTCOME_APP_PROMOTION": gettext_lazy("App promotion"),
}
# What the simplified creation offers, and how each is delivered.
CREATABLE_OBJECTIVES = ("OUTCOME_AWARENESS", "OUTCOME_TRAFFIC", "OUTCOME_LEADS", "OUTCOME_SALES")
PIXEL_OBJECTIVES = {"OUTCOME_SALES": "PURCHASE", "OUTCOME_LEADS": "LEAD"}
OPTIMIZATION_GOALS = {
    "OUTCOME_AWARENESS": "REACH",
    "OUTCOME_TRAFFIC": "LINK_CLICKS",
    "OUTCOME_LEADS": "OFFSITE_CONVERSIONS",
    "OUTCOME_SALES": "OFFSITE_CONVERSIONS",
}

CALLS_TO_ACTION = {
    "LEARN_MORE": gettext_lazy("Learn more"),
    "SHOP_NOW": gettext_lazy("Shop now"),
    "SIGN_UP": gettext_lazy("Sign up"),
    "CONTACT_US": gettext_lazy("Contact us"),
    "SUBSCRIBE": gettext_lazy("Subscribe"),
    "GET_OFFER": gettext_lazy("Get offer"),
}

# ``effective_status`` → (label, pill tone).
EFFECTIVE_STATUSES = {
    "ACTIVE": (gettext_lazy("Active"), "success"),
    "PAUSED": (gettext_lazy("Paused"), "neutral"),
    "CAMPAIGN_PAUSED": (gettext_lazy("Campaign paused"), "neutral"),
    "ADSET_PAUSED": (gettext_lazy("Ad set paused"), "neutral"),
    "IN_PROCESS": (gettext_lazy("Processing"), "info"),
    "PENDING_REVIEW": (gettext_lazy("In review"), "info"),
    "PREAPPROVED": (gettext_lazy("Approved"), "info"),
    "WITH_ISSUES": (gettext_lazy("With issues"), "warning"),
    "PENDING_BILLING_INFO": (gettext_lazy("Billing pending"), "warning"),
    "DISAPPROVED": (gettext_lazy("Rejected"), "error"),
    "ARCHIVED": (gettext_lazy("Archived"), "neutral"),
    "DELETED": (gettext_lazy("Deleted"), "neutral"),
}

# Reading windows offered on the screens, in days.
RANGE_CHOICES = (7, 14, 30, 90)
DEFAULT_RANGE = 30

IMAGE_MAX_BYTES = 30 * 1024 * 1024


class AdsError(Exception):
    """Something the person can read and act on."""


class AdsNotConfiguredError(AdsError):
    pass


# ------------------------------------------------------------------
# Credentials and errors
# ------------------------------------------------------------------


def app_credentials(org_id) -> dict:
    """The Meta app's id and secret, plus the optional Login for Business config for ads.

    Raises ``AdsNotConfiguredError`` when the app is not set up. The Facebook Pages
    ``config_id`` is deliberately not reused: a Login for Business configuration
    fixes its permission list, and the Pages one has no ads permissions.
    """
    creds = resolve_platform_credentials(CREDENTIAL_PLATFORM, org_id)
    client_id = str(creds.get("client_id") or creds.get("app_id") or "").strip()
    client_secret = str(creds.get("client_secret") or creds.get("app_secret") or "").strip()
    if not client_id or not client_secret:
        raise AdsNotConfiguredError(_("The Meta app is not configured. Ask your administrator to set it up."))
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "ads_config_id": settings.PLATFORM_META_ADS_CONFIG_ID,
    }


def is_configured(org_id) -> bool:
    try:
        app_credentials(org_id)
    except AdsNotConfiguredError:
        return False
    return True


def client_for_org(org_id) -> meta_ads.MetaAdsClient:
    return meta_ads.MetaAdsClient(app_credentials(org_id))


def describe_error(exc: ProviderError) -> str:
    if isinstance(exc, TokenExpiredError):
        return _("The Meta sign-in expired or was revoked. Connect Meta Ads again.")
    if isinstance(exc, RateLimitError):
        return _("Meta is limiting requests to this ad account right now. Try again in a few minutes.")
    detail = str(exc).removeprefix("Meta Ads: ").strip()
    return _("Meta refused the request: %(detail)s") % {"detail": detail or _("no reason given")}


def _record_failure(connection: AdsConnection, exc: ProviderError) -> None:
    """A lapsed token turns the connection to ``error``; anything else leaves it as it was."""
    if isinstance(exc, TokenExpiredError):
        connection.status = AdsConnection.Status.ERROR
        connection.last_error = describe_error(exc)
        connection.save(update_fields=["status", "last_error", "updated_at"])


def _call(connection: AdsConnection, fn, *args, **kwargs):
    """Run a client call with the connection's token; Meta's refusal becomes an ``AdsError``.

    The client is kept on the connection object, so the several calls one page
    makes resolve the app credentials once.
    """
    client = getattr(connection, "_ads_client", None)
    if client is None:
        client = client_for_org(connection.workspace.organization_id)
        connection._ads_client = client  # type: ignore[attr-defined]
    try:
        return fn(client, connection.access_token, *args, **kwargs)
    except ProviderError as exc:
        logger.warning("Meta Ads call %s failed for workspace %s: %s", fn.__name__, connection.workspace_id, exc)
        _record_failure(connection, exc)
        raise AdsError(describe_error(exc)) from exc


# ------------------------------------------------------------------
# Connection
# ------------------------------------------------------------------


def get_connection(workspace_id) -> AdsConnection | None:
    return AdsConnection.objects.for_workspace(workspace_id).select_related("workspace").first()


def finish_sign_in(*, workspace, user, code: str, redirect_uri: str) -> AdsConnection:
    """Trade the Login dialog's code for a long-lived token and keep it on the workspace.

    A reconnect keeps the ad account, Pixel and Page already chosen, as long as
    the new token still reaches that ad account; otherwise the choice starts over.
    """
    creds = app_credentials(workspace.organization_id)
    facebook = FacebookProvider(creds)
    try:
        short = facebook.exchange_code(code, redirect_uri)
        try:
            tokens = facebook.refresh_token(short.access_token)
        except ProviderError:
            logger.warning("Meta Ads long-lived token exchange failed; keeping the short-lived token")
            tokens = short
        profile = facebook.get_profile(tokens.access_token)
        reachable = {account.id for account in meta_ads.MetaAdsClient(creds).ad_accounts(tokens.access_token)}
    except ProviderError as exc:
        logger.warning("Meta Ads sign-in failed for workspace %s: %s", workspace.id, exc)
        raise AdsError(describe_error(exc)) from exc

    expires_at = timezone.now() + timedelta(seconds=tokens.expires_in) if tokens.expires_in else None
    with transaction.atomic():
        connection = AdsConnection.objects.select_for_update().filter(workspace=workspace).first()
        if connection is None:
            connection = AdsConnection(workspace=workspace)
        connection.access_token = tokens.access_token
        connection.token_expires_at = expires_at
        connection.meta_user_id = profile.platform_id
        connection.meta_user_name = profile.name
        connection.connected_by = user
        connection.last_error = ""
        if connection.ad_account_id and connection.ad_account_id in reachable:
            connection.status = AdsConnection.Status.CONNECTED
        else:
            _clear_assets(connection)
            connection.status = AdsConnection.Status.PENDING
        connection.save()
    return connection


def _clear_assets(connection: AdsConnection) -> None:
    connection.ad_account_id = connection.ad_account_name = ""
    connection.currency = connection.account_timezone = ""
    connection.pixel_id = connection.pixel_name = ""
    connection.page_id = connection.page_name = ""


@dataclass
class AssetChoices:
    accounts: list[meta_ads.AdAccount]
    pixels: list[meta_ads.Pixel]
    pages: list[meta_ads.Page]
    account: meta_ads.AdAccount | None


def asset_choices(connection: AdsConnection, ad_account_id: str = "") -> AssetChoices:
    """What the token reaches: the ad accounts, and the Pixels and Pages of the chosen one.

    Pixels and Pages come only from that account, so a Pixel or Page wired to
    another account cannot be paired with it and skew its results.
    """
    accounts = _call(connection, meta_ads.MetaAdsClient.ad_accounts)
    wanted = ad_account_id or connection.ad_account_id
    account = next((a for a in accounts if a.id == wanted), None)
    if account is None and len(accounts) == 1:
        account = accounts[0]
    pixels = _call(connection, meta_ads.MetaAdsClient.pixels, account.id) if account else []
    pages = _call(connection, meta_ads.MetaAdsClient.pages, account.id) if account else []
    return AssetChoices(accounts=accounts, pixels=pixels, pages=pages, account=account)


def choose_assets(connection: AdsConnection, *, ad_account_id: str, pixel_id: str, page_id: str) -> AdsConnection:
    """Keep the ad account, Pixel and Page — each only if this token really reaches it."""
    choices = asset_choices(connection, ad_account_id)
    account = choices.account if choices.account and choices.account.id == ad_account_id else None
    if account is None:
        raise AdsError(_("Choose one of the ad accounts your Meta sign-in can manage."))
    pixel = next((p for p in choices.pixels if p.id == pixel_id), None) if pixel_id else None
    if pixel_id and pixel is None:
        raise AdsError(_("That Pixel does not belong to the chosen ad account."))
    page = next((p for p in choices.pages if p.id == page_id), None) if page_id else None
    if page_id and page is None:
        raise AdsError(_("Choose one of the Pages your Meta sign-in manages."))

    connection.ad_account_id = account.id
    connection.ad_account_name = account.name
    connection.currency = account.currency
    connection.account_timezone = account.timezone
    connection.pixel_id = pixel.id if pixel else ""
    connection.pixel_name = pixel.name if pixel else ""
    connection.page_id = page.id if page else ""
    connection.page_name = page.name if page else ""
    connection.status = AdsConnection.Status.CONNECTED
    connection.last_error = ""
    connection.save()
    return connection


def disconnect(connection: AdsConnection) -> None:
    """Forget the token. Campaigns keep running on Meta: they belong to the ad account."""
    connection.delete()


# ------------------------------------------------------------------
# Campaigns
# ------------------------------------------------------------------


@dataclass
class Campaign:
    id: str
    name: str
    objective: str
    objective_label: str
    status: str  # what was set: ACTIVE / PAUSED / ARCHIVED
    effective_status: str
    status_label: str
    status_tone: str
    daily_budget: Decimal | None
    lifetime_budget: Decimal | None
    start_time: datetime | None
    stop_time: datetime | None
    insights: meta_ads.Insights = field(default_factory=meta_ads.Insights)
    # The same numbers for the window of equal length just before.
    previous: meta_ads.Insights = field(default_factory=meta_ads.Insights)

    @property
    def is_active(self) -> bool:
        return self.status == "ACTIVE"

    @property
    def can_toggle(self) -> bool:
        return self.status in ("ACTIVE", "PAUSED")

    @property
    def key_metric(self) -> str:
        """The number that says whether this campaign works, given what it is for."""
        return KEY_METRICS.get(self.objective, "cpm")

    @property
    def key_metric_label(self) -> str:
        return str(KEY_METRIC_LABELS[self.key_metric])

    @property
    def key_metric_short(self) -> str:
        """The label a table column has room for."""
        return str(KEY_METRIC_SHORT_LABELS[self.key_metric])

    @property
    def key_value(self) -> Decimal | None:
        return metric_value(self.insights, self.key_metric)

    @property
    def key_change(self) -> Change | None:
        return compare(self.insights, self.previous, self.key_metric)

    @property
    def pays_back(self) -> bool | None:
        """Sales campaigns only: whether the purchases brought back at least what was spent."""
        if self.objective != "OUTCOME_SALES" or self.insights.roas is None:
            return None
        return self.insights.roas >= 1


# ------------------------------------------------------------------
# Comparison with the previous window
# ------------------------------------------------------------------

# Which way a metric is good news. ``None``: neither, spending more is a choice
# rather than a result, so its change is shown without a colour.
BETTER = {
    "spend": None,
    "impressions": "higher",
    "reach": "higher",
    "link_clicks": "higher",
    "ctr": "higher",
    "cpc": "lower",
    "cpm": "lower",
    "purchases": "higher",
    "leads": "higher",
    "roas": "higher",
    "cost_per_lead": "lower",
}

# objective → the metric a campaign of that objective is judged by.
KEY_METRICS = {
    "OUTCOME_SALES": "roas",
    "OUTCOME_LEADS": "cost_per_lead",
    "OUTCOME_TRAFFIC": "cpc",
    "OUTCOME_ENGAGEMENT": "cpc",
    "OUTCOME_AWARENESS": "cpm",
}

KEY_METRIC_LABELS = {
    "roas": _("Return on ad spend"),
    "cost_per_lead": _("Cost per lead"),
    "cpc": _("Cost per click"),
    "cpm": _("Cost per 1,000 impressions"),
}
KEY_METRIC_SHORT_LABELS = {
    "roas": _("ROAS"),
    "cost_per_lead": _("Cost per lead"),
    "cpc": _("CPC"),
    "cpm": _("CPM"),
}


@dataclass(frozen=True)
class Change:
    """How a number moved against the previous window: ``delta`` in %, ``tone`` good/bad/neutral.

    ``from_nothing``: the previous window had no such number (no purchases, no
    clicks), so any value now reads as +100% and is not judged good or bad.
    """

    delta: float
    tone: str
    from_nothing: bool = False


def metric_value(insights: meta_ads.Insights, key: str) -> Decimal | None:
    if key == "cost_per_lead":
        return insights.spend / insights.leads if insights.leads else None
    value = getattr(insights, key)
    return None if value is None else Decimal(value)


def compare(current: meta_ads.Insights, previous: meta_ads.Insights, key: str) -> Change | None:
    """The change of ``key`` between the two windows; None only when there is no value now."""
    now, before = metric_value(current, key), metric_value(previous, key)
    if now is None:
        return None
    if not before:
        return Change(100.0 if now else 0.0, "neutral", from_nothing=True)
    delta = round(float((now - before) / before * 100), 1)
    better = BETTER[key]
    if not delta or better is None:
        return Change(delta, "neutral")
    return Change(delta, "good" if (delta > 0) == (better == "higher") else "bad")


def compare_all(current: meta_ads.Insights, previous: meta_ads.Insights) -> dict[str, Change | None]:
    return {key: compare(current, previous, key) for key in BETTER}


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None


def _status(effective: str) -> tuple[str, str]:
    label, tone = EFFECTIVE_STATUSES.get(effective, (effective.replace("_", " ").title(), "neutral"))
    return str(label), tone


def _campaign_from_row(row: dict, currency: str) -> Campaign:
    effective = row.get("effective_status") or row.get("status") or ""
    label, tone = _status(effective)
    objective = row.get("objective") or ""
    return Campaign(
        id=str(row.get("id")),
        name=row.get("name") or "",
        objective=objective,
        objective_label=str(OBJECTIVES.get(objective, objective)),
        status=row.get("status") or "",
        effective_status=effective,
        status_label=label,
        status_tone=tone,
        daily_budget=meta_ads.from_minor_units(row.get("daily_budget"), currency),
        lifetime_budget=meta_ads.from_minor_units(row.get("lifetime_budget"), currency),
        start_time=_parse_time(row.get("start_time")),
        stop_time=_parse_time(row.get("stop_time")),
    )


def date_window(days: int) -> tuple[str, str]:
    today = timezone.localdate()
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def previous_window(days: int) -> tuple[str, str]:
    """The ``days`` days just before :func:`date_window`, what its numbers are compared with."""
    today = timezone.localdate()
    return (today - timedelta(days=2 * days - 1)).isoformat(), (today - timedelta(days=days)).isoformat()


def _per_campaign(connection: AdsConnection, since: str, until: str) -> dict[str, meta_ads.Insights]:
    rows = _call(
        connection,
        meta_ads.MetaAdsClient.insights,
        connection.ad_account_id,
        since=since,
        until=until,
        level="campaign",
    )
    return {row.campaign_id: row for row in rows}


def list_campaigns(connection: AdsConnection, *, days: int = DEFAULT_RANGE) -> list[Campaign]:
    """The ad account's campaigns (archived ones last), each with its numbers for the window
    and for the window before it."""
    rows = _call(connection, meta_ads.MetaAdsClient.campaigns, connection.ad_account_id)
    campaigns = [_campaign_from_row(row, connection.currency) for row in rows]
    current = _per_campaign(connection, *date_window(days))
    previous = _per_campaign(connection, *previous_window(days))
    for campaign in campaigns:
        campaign.insights = current.get(campaign.id, meta_ads.Insights())
        campaign.previous = previous.get(campaign.id, meta_ads.Insights())
    campaigns.sort(key=lambda c: (c.status in ("ARCHIVED", "DELETED"), c.status != "ACTIVE"))
    return campaigns


def _numeric_account_id(connection: AdsConnection) -> str:
    return connection.ad_account_id.removeprefix("act_")


def get_campaign(connection: AdsConnection, campaign_id: str) -> tuple[Campaign, dict]:
    """One campaign of *this* ad account, or ``AdsError``.

    A campaign id from the URL is never trusted on its own: the token may reach
    other ad accounts, so the campaign's ``account_id`` has to be the chosen one.
    """
    if not campaign_id.isdigit():
        raise AdsError(_("This campaign was not found in the connected ad account."))
    row = _call(connection, meta_ads.MetaAdsClient.campaign, campaign_id)
    if str(row.get("account_id") or "") != _numeric_account_id(connection):
        raise AdsError(_("This campaign was not found in the connected ad account."))
    return _campaign_from_row(row, connection.currency), row


@dataclass
class AdSet:
    id: str
    name: str
    status_label: str
    status_tone: str
    daily_budget: Decimal | None
    lifetime_budget: Decimal | None
    end_time: datetime | None
    optimization_goal: str
    countries: list[str]
    age_min: int | None
    age_max: int | None


def campaign_detail(connection: AdsConnection, campaign_id: str, *, days: int = DEFAULT_RANGE) -> dict:
    campaign, _row = get_campaign(connection, campaign_id)
    ad_sets = []
    for row in _call(connection, meta_ads.MetaAdsClient.ad_sets, campaign.id):
        targeting = row.get("targeting") or {}
        label, tone = _status(row.get("effective_status") or row.get("status") or "")
        ad_sets.append(
            AdSet(
                id=str(row.get("id")),
                name=row.get("name") or "",
                status_label=label,
                status_tone=tone,
                daily_budget=meta_ads.from_minor_units(row.get("daily_budget"), connection.currency),
                lifetime_budget=meta_ads.from_minor_units(row.get("lifetime_budget"), connection.currency),
                end_time=_parse_time(row.get("end_time")),
                optimization_goal=row.get("optimization_goal") or "",
                countries=list((targeting.get("geo_locations") or {}).get("countries") or []),
                age_min=targeting.get("age_min"),
                age_max=targeting.get("age_max"),
            )
        )
    ads = []
    for row in _call(connection, meta_ads.MetaAdsClient.ads, campaign.id):
        label, tone = _status(row.get("effective_status") or row.get("status") or "")
        creative = row.get("creative") or {}
        ads.append(
            {
                "id": row.get("id"),
                "name": row.get("name") or "",
                "status_label": label,
                "status_tone": tone,
                "thumbnail_url": creative.get("thumbnail_url") or "",
                # Meta's preview page: the ad as people see it, in each placement.
                "preview_url": _https_url(row.get("preview_shareable_link")),
            }
        )
    since, until = date_window(days)
    totals = _first(_call(connection, meta_ads.MetaAdsClient.insights, campaign.id, since=since, until=until))
    before_since, before_until = previous_window(days)
    before = _first(
        _call(connection, meta_ads.MetaAdsClient.insights, campaign.id, since=before_since, until=before_until)
    )
    daily = _call(connection, meta_ads.MetaAdsClient.insights, campaign.id, since=since, until=until, daily=True)
    campaign.insights, campaign.previous = totals, before
    return {
        "campaign": campaign,
        "ad_sets": ad_sets,
        "ads": ads,
        "totals": totals,
        "changes": compare_all(totals, before),
        "series": daily_series(daily, since, until),
        "breakdowns": campaign_breakdowns(connection, campaign, ads, days=days),
    }


def _https_url(value) -> str:
    """``value`` when it is an https link, else "": it goes into an href."""
    value = str(value or "")
    return value if value.startswith("https://") else ""


def _first(rows: list[meta_ads.Insights]) -> meta_ads.Insights:
    return rows[0] if rows else meta_ads.Insights()


# ------------------------------------------------------------------
# Where a campaign's results come from
# ------------------------------------------------------------------

# A row needs at least this share of the campaign's spend to be called the best:
# a placement that spent R$ 2 and sold once would otherwise always win.
BEST_MIN_SPEND_SHARE = Decimal("0.1")

GENDERS = {"male": _("Men"), "female": _("Women"), "unknown": _("Not informed")}
PLATFORMS = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "messenger": "Messenger",
    "audience_network": "Audience Network",
    "threads": "Threads",
}
POSITIONS = {
    "feed": _("Feed"),
    "story": _("Stories"),
    "instagram_stories": _("Stories"),
    "facebook_stories": _("Stories"),
    "reels": _("Reels"),
    "instagram_reels": _("Reels"),
    "facebook_reels": _("Reels"),
    "facebook_reels_overlay": _("Reels overlay"),
    "explore": _("Explore"),
    "explore_home": _("Explore home"),
    "instagram_explore_grid_home": _("Explore home"),
    "profile_feed": _("Profile feed"),
    "instagram_profile_feed": _("Profile feed"),
    "marketplace": _("Marketplace"),
    "video_feeds": _("Video feeds"),
    "instream_video": _("In-stream video"),
    "right_hand_column": _("Right column"),
    "search": _("Search results"),
    "messenger_inbox": _("Inbox"),
    "an_classic": _("Apps and sites"),
    "rewarded_video": _("Rewarded video"),
}


@dataclass
class BreakdownRow:
    """One slice of a campaign's results: an ad, an age and gender, or a placement."""

    label: str
    insights: meta_ads.Insights
    key_value: Decimal | None = None
    is_best: bool = False
    thumbnail_url: str = ""
    preview_url: str = ""


def _placement_label(extra: dict) -> str:
    platform = extra.get("publisher_platform", "")
    position = extra.get("platform_position", "")
    platform_label = PLATFORMS.get(platform, platform.replace("_", " ").title())
    position_label = str(POSITIONS.get(position, position.replace("_", " ").capitalize()))
    return f"{platform_label} · {position_label}" if position_label else platform_label


def _audience_label(extra: dict) -> str:
    gender = extra.get("gender", "")
    return f"{extra.get('age', '')} · {GENDERS.get(gender, gender)}"


def _rank(rows: list[BreakdownRow], key: str, total_spend: Decimal) -> list[BreakdownRow]:
    """Rows by spend, the best one by the campaign's own metric marked."""
    for row in rows:
        row.key_value = metric_value(row.insights, key)
    floor = total_spend * BEST_MIN_SPEND_SHARE
    candidates = [r for r in rows if r.key_value is not None and r.key_value > 0 and r.insights.spend >= floor]
    if len(candidates) > 1:
        pick = max if BETTER[key] == "higher" else min
        pick(candidates, key=lambda r: r.key_value or Decimal(0)).is_best = True
    return sorted(rows, key=lambda r: r.insights.spend, reverse=True)


def campaign_breakdowns(connection: AdsConnection, campaign: Campaign, ads: list[dict], *, days: int) -> dict:
    """Which ads, audiences (age and gender) and placements bring a campaign's results.

    Meta Ads has no keywords: what an advertiser compares instead is the
    creative, who saw it and where. Each list is ranked by the metric the
    campaign is judged by (``Campaign.key_metric``).
    """
    since, until = date_window(days)

    def rows(**kwargs) -> list[meta_ads.Insights]:
        return _call(connection, meta_ads.MetaAdsClient.insights, campaign.id, since=since, until=until, **kwargs)

    by_id = {str(ad["id"]): ad for ad in ads}
    per_ad = []
    for row in rows(level="ad"):
        ad = by_id.get(row.extra.get("ad_id", ""), {})
        per_ad.append(
            BreakdownRow(
                label=row.extra.get("ad_name", ""),
                insights=row,
                thumbnail_url=ad.get("thumbnail_url", ""),
                preview_url=ad.get("preview_url", ""),
            )
        )
    audience = [BreakdownRow(label=_audience_label(row.extra), insights=row) for row in rows(breakdowns="age,gender")]
    placements = [
        BreakdownRow(label=_placement_label(row.extra), insights=row)
        for row in rows(breakdowns="publisher_platform,platform_position")
    ]
    key = campaign.key_metric
    total = campaign.insights.spend
    return {
        "key_metric": key,
        "key_metric_label": str(KEY_METRIC_LABELS[key]),
        "key_metric_short": str(KEY_METRIC_SHORT_LABELS[key]),
        "ads": _rank(per_ad, key, total),
        "audience": _rank(audience, key, total),
        "placements": _rank(placements, key, total),
    }


def set_campaign_status(connection: AdsConnection, campaign_id: str, status: str) -> Campaign:
    """Start (``ACTIVE``) or stop (``PAUSED``) a campaign; ``ARCHIVED`` retires it for good."""
    if status not in ("ACTIVE", "PAUSED", "ARCHIVED"):
        raise AdsError(_("Unknown campaign status."))
    campaign, _row = get_campaign(connection, campaign_id)
    _call(connection, meta_ads.MetaAdsClient.update, campaign.id, {"status": status})
    campaign.status = status
    return campaign


def update_campaign(connection: AdsConnection, campaign_id: str, *, name: str) -> None:
    campaign, _row = get_campaign(connection, campaign_id)
    _call(connection, meta_ads.MetaAdsClient.update, campaign.id, {"name": name})


def update_ad_set(
    connection: AdsConnection,
    campaign_id: str,
    ad_set_id: str,
    *,
    daily_budget: Decimal | None,
    end_time: datetime | None,
) -> None:
    """Change an ad set's daily budget and end; the ad set must belong to the campaign."""
    campaign, _row = get_campaign(connection, campaign_id)
    ad_sets = {str(row.get("id")): row for row in _call(connection, meta_ads.MetaAdsClient.ad_sets, campaign.id)}
    row = ad_sets.get(ad_set_id)
    if row is None:
        raise AdsError(_("This ad set is not part of the campaign."))
    fields: dict = {}
    if daily_budget is not None:
        if row.get("lifetime_budget") not in (None, "", "0"):
            raise AdsError(_("This ad set has a lifetime budget. Change it in Meta Ads Manager."))
        fields["daily_budget"] = meta_ads.to_minor_units(daily_budget, connection.currency)
    if end_time is not None:
        fields["end_time"] = end_time.isoformat()
    if fields:
        _call(connection, meta_ads.MetaAdsClient.update, ad_set_id, fields)


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
    age_min: int
    age_max: int
    genders: list[int]  # [] all, [1] men, [2] women
    image_bytes: bytes
    message: str
    headline: str
    link: str
    call_to_action: str


def build_ad_set_payload(connection: AdsConnection, new: NewCampaign, campaign_id: str) -> dict:
    """The ad set Meta needs for the objective: what it optimises for, budget, audience."""
    targeting: dict = {
        "geo_locations": {"countries": new.countries},
        "age_min": new.age_min,
        "age_max": new.age_max,
        # Explicit from v23 on: without it Meta refuses the ad set. 0 keeps the
        # age and gender chosen here as limits rather than suggestions.
        "targeting_automation": {"advantage_audience": 0},
    }
    if new.genders:
        targeting["genders"] = new.genders
    payload = {
        "name": f"{new.name} — {_('Ad set')}",
        "campaign_id": campaign_id,
        "daily_budget": meta_ads.to_minor_units(new.daily_budget, connection.currency),
        "billing_event": "IMPRESSIONS",
        "optimization_goal": OPTIMIZATION_GOALS[new.objective],
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "targeting": json.dumps(targeting),
    }
    event = PIXEL_OBJECTIVES.get(new.objective)
    if event:
        if not connection.pixel_id:
            raise AdsError(_("Choose a Pixel in the connection settings to run sales or lead campaigns."))
        payload["promoted_object"] = json.dumps({"pixel_id": connection.pixel_id, "custom_event_type": event})
    if new.start_time:
        payload["start_time"] = new.start_time.isoformat()
    if new.end_time:
        payload["end_time"] = new.end_time.isoformat()
    return payload


def build_story_spec(connection: AdsConnection, new: NewCampaign, image_hash: str) -> dict:
    return {
        "page_id": connection.page_id,
        "link_data": {
            "link": new.link,
            "message": new.message,
            "name": new.headline,
            "image_hash": image_hash,
            "call_to_action": {"type": new.call_to_action, "value": {"link": new.link}},
        },
    }


def read_image(asset) -> bytes:
    """The bytes of a library image, refused above Meta's upload size."""
    if asset.file_size and asset.file_size > IMAGE_MAX_BYTES:
        raise AdsError(_("The image is larger than Meta accepts (30 MB)."))
    with asset.file.open("rb") as handle:
        content = handle.read(IMAGE_MAX_BYTES + 1)
    if len(content) > IMAGE_MAX_BYTES:
        raise AdsError(_("The image is larger than Meta accepts (30 MB)."))
    return content


def create_campaign(connection: AdsConnection, new: NewCampaign) -> str:
    """Create campaign, ad set, image, creative and ad; return the campaign id.

    The campaign is created paused and stays paused until someone activates it.
    A failure part-way deletes the campaign again (which takes its ad set and ad
    with it), so a refused creative never leaves an empty campaign behind.
    """
    if not connection.is_ready:
        raise AdsError(_("Finish connecting Meta Ads before creating campaigns."))
    if not connection.page_id:
        raise AdsError(_("Choose the Facebook Page your ads are published as in the connection settings."))
    if new.objective not in CREATABLE_OBJECTIVES:
        raise AdsError(_("Choose one of the offered objectives."))
    if new.objective in PIXEL_OBJECTIVES and not connection.pixel_id:
        raise AdsError(_("Choose a Pixel in the connection settings to run sales or lead campaigns."))
    account = connection.ad_account_id
    campaign_id = _call(
        connection, meta_ads.MetaAdsClient.create_campaign, account, name=new.name, objective=new.objective
    )
    try:
        ad_set_id = _call(
            connection,
            meta_ads.MetaAdsClient.create_ad_set,
            account,
            {**build_ad_set_payload(connection, new, campaign_id), "status": "ACTIVE"},
        )
        image_hash = _call(connection, meta_ads.MetaAdsClient.upload_image, account, new.image_bytes)
        creative_id = _call(
            connection,
            meta_ads.MetaAdsClient.create_creative,
            account,
            name=new.name,
            object_story_spec=build_story_spec(connection, new, image_hash),
        )
        _call(
            connection,
            meta_ads.MetaAdsClient.create_ad,
            account,
            name=new.name,
            ad_set_id=ad_set_id,
            creative_id=creative_id,
            status="ACTIVE",
        )
    except AdsError:
        try:
            _call(connection, meta_ads.MetaAdsClient.delete, campaign_id)
        except AdsError:
            logger.error("Meta Ads campaign %s was left half-created and could not be deleted", campaign_id)
        raise
    return campaign_id


# ------------------------------------------------------------------
# Insights
# ------------------------------------------------------------------


def daily_series(rows: list[meta_ads.Insights], since: str, until: str) -> dict[str, list]:
    """Spend, impressions and clicks per day over the whole window; missing days are 0."""
    by_day = {row.date_start: row for row in rows}
    start, end = date.fromisoformat(since), date.fromisoformat(until)
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    return {
        "days": days,
        "spend": [float(by_day[d].spend) if d in by_day else 0 for d in days],
        "impressions": [by_day[d].impressions if d in by_day else 0 for d in days],
        "clicks": [by_day[d].link_clicks if d in by_day else 0 for d in days],
    }


def account_insights(connection: AdsConnection, *, since: str, until: str, daily: bool = False) -> list:
    """The ad account's insights for ``since``..``until``: one total row, or one row per day."""
    return _call(
        connection, meta_ads.MetaAdsClient.insights, connection.ad_account_id, since=since, until=until, daily=daily
    )


def account_overview(connection: AdsConnection, *, days: int = DEFAULT_RANGE) -> dict:
    """The ad account's totals and daily series for the window, and how each total
    moved against the window before it."""
    since, until = date_window(days)
    totals = _first(account_insights(connection, since=since, until=until))
    before_since, before_until = previous_window(days)
    before = _first(account_insights(connection, since=before_since, until=before_until))
    daily = account_insights(connection, since=since, until=until, daily=True)
    return {"totals": totals, "changes": compare_all(totals, before), "series": daily_series(daily, since, until)}
