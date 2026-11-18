"""Shopee Affiliates analytics: the orders the workspace's links brought.

Shaped like the analytics page's own payloads (``apps.analytics``): KPI cards
and the hero chart are :class:`~apps.analytics.derive.DerivedMetric` built by
the same ``derive``, so the shared card, chart and delta partials render them.

Read live from Shopee's ``conversionReport`` and cached briefly: Shopee only
answers for the last ``REPORT_MAX_DAYS`` days, which is also why a range whose
previous period falls outside that reach has no comparison.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo
from decimal import Decimal

from django.core.cache import cache
from django.utils import formats, timezone
from django.utils.translation import gettext_lazy as _

from apps.analytics.derive import DerivedMetric, derive
from providers.exceptions import ProviderError
from providers.shopee_affiliate import (
    ORDER_CANCELLED,
    ORDER_COMPLETED,
    ORDER_PENDING,
    REPORT_MAX_DAYS,
    Conversion,
    ConversionReport,
)

from .models import OpportunityConnection
from .services import _record_health, shopee_client

REPORT_CACHE_SECONDS = 15 * 60
TABLE_PAGE_SIZE = 20
TOP_PRODUCTS = 5

# key → (label, kind). ``kind`` is what ``fmt_metric`` formats by.
METRICS = {
    "commission": (_("Commission"), "currency"),
    "orders": (_("Orders"), "count"),
    "sales": (_("Sales"), "currency"),
    "items": (_("Items sold"), "count"),
}
PRIMARY_METRIC = "commission"

STATUSES = {
    ORDER_COMPLETED: _("Completed"),
    ORDER_PENDING: _("Pending"),
    ORDER_CANCELLED: _("Cancelled"),
}
STATUS_FILTERS = ("all", ORDER_COMPLETED, ORDER_PENDING, ORDER_CANCELLED)
# ``.a-delta`` tone of each status on the analytics page.
STATUS_TONES = {ORDER_COMPLETED: "good", ORDER_PENDING: "neutral", ORDER_CANCELLED: "bad"}

# Shopee's ``referrer`` values that read badly as they come.
REFERRERS = {
    "Shopeevideo-Shopee": _("Shopee Video"),
    "Websites": _("Websites"),
    "Others": _("Others"),
}


def load_conversions(connection: OpportunityConnection) -> ConversionReport:
    """Every order of the last ``REPORT_MAX_DAYS`` days, cached for a few minutes."""
    key = f"radar:shopee:conversions:{connection.pk}:{connection.updated_at.timestamp()}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    end = timezone.now()
    try:
        report = shopee_client(connection).conversions(end - timedelta(days=REPORT_MAX_DAYS), end)
    except ProviderError as exc:
        _record_health(connection, exc)
        raise
    _record_health(connection, None)
    cache.set(key, report, REPORT_CACHE_SECONDS)
    return report


@dataclass(frozen=True)
class Window:
    """The ``2 * days`` local dates a range is derived over, oldest first."""

    dates: list[date]
    days: int

    @property
    def current(self) -> list[date]:
        return self.dates[-self.days :]

    def index(self, day: date) -> int | None:
        offset = (day - self.dates[0]).days
        return offset if 0 <= offset < len(self.dates) else None


def _window(days: int, today: date) -> Window:
    return Window(dates=[today - timedelta(days=2 * days - 1 - i) for i in range(2 * days)], days=days)


def _counts(conversion: Conversion) -> bool:
    """A cancelled order earns nothing and sold nothing; it is only counted as cancelled."""
    return conversion.status != ORDER_CANCELLED


def _series(conversions: list[Conversion], window: Window, tz: tzinfo) -> dict[str, list[float]]:
    series = {key: [0.0] * len(window.dates) for key in METRICS}
    for conversion in conversions:
        i = window.index(conversion.purchased_at.astimezone(tz).date())
        if i is None or not _counts(conversion):
            continue
        series["commission"][i] += float(conversion.commission)
        series["orders"][i] += 1
        series["sales"][i] += float(conversion.amount)
        series["items"][i] += conversion.quantity
    return series


def _derived(values: list[float], days: int, kind: str, comparable: bool) -> DerivedMetric:
    metric = derive(values, days, kind)
    metric.comparison_available = comparable
    return metric


def referrer_label(referrer: str) -> str:
    return str(REFERRERS.get(referrer, referrer or _("Unknown")))


def _status_summary(conversions: list[Conversion]) -> list[dict]:
    summary = []
    for status, label in STATUSES.items():
        matching = [c for c in conversions if c.status == status]
        summary.append(
            {
                "key": status,
                "label": label,
                "tone": STATUS_TONES[status],
                "orders": len(matching),
                "commission": sum((c.commission for c in matching), Decimal(0)),
            }
        )
    return summary


def _top_products(conversions: list[Conversion]) -> list[dict]:
    totals: dict[int, dict] = defaultdict(lambda: {"orders": 0, "commission": Decimal(0)})
    for c in conversions:
        if not _counts(c):
            continue
        entry = totals[c.item_id]
        entry.update(name=c.item_name, image_url=c.image_url, shop_name=c.shop_name)
        entry["orders"] += 1
        entry["commission"] += c.commission
    ranked = sorted(totals.values(), key=lambda e: (e["commission"], e["orders"]), reverse=True)
    return ranked[:TOP_PRODUCTS]


def _referrers(conversions: list[Conversion]) -> list[dict]:
    counted = Counter(referrer_label(c.referrer) for c in conversions if _counts(c))
    total = sum(counted.values())
    return [
        {"label": label, "orders": n, "share": round(n * 100 / total) if total else 0}
        for label, n in counted.most_common()
    ]


def _table(conversions: list[Conversion], *, status: str, page: int, tz: tzinfo) -> dict:
    rows = [c for c in conversions if status == "all" or c.status == status]
    rows.sort(key=lambda c: c.purchased_at, reverse=True)
    total = len(rows)
    total_pages = max(1, math.ceil(total / TABLE_PAGE_SIZE))
    page = min(max(1, page), total_pages)
    start = (page - 1) * TABLE_PAGE_SIZE
    return {
        "rows": [
            {
                "conversion": c,
                "date": formats.date_format(c.purchased_at.astimezone(tz), "SHORT_DATETIME_FORMAT"),
                "status_label": STATUSES.get(c.status, c.status.title()),
                "status_tone": STATUS_TONES.get(c.status, "neutral"),
                "referrer": referrer_label(c.referrer),
            }
            for c in rows[start : start + TABLE_PAGE_SIZE]
        ],
        "status": status,
        "statuses": [("all", _("All"))] + list(STATUSES.items()),
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "page_from": start + 1 if total else 0,
        "page_to": min(start + TABLE_PAGE_SIZE, total),
    }


def build_report(
    connection: OpportunityConnection,
    *,
    days: int,
    tz: tzinfo,
    end: date | None = None,
    metric: str | None = None,
    status: str = "all",
    page: int = 1,
) -> dict:
    """Everything the Shopee analytics page shows for the last ``days`` days, or
    the ``days`` ending on ``end``.

    Raises :class:`~providers.exceptions.ProviderError` when Shopee fails.
    """
    report = load_conversions(connection)
    today = timezone.now().astimezone(tz).date()
    window = _window(days, end or today)
    # The previous period is only comparable if Shopee still reports all of it.
    comparable = window.dates[0] > today - timedelta(days=REPORT_MAX_DAYS)
    series = _series(report.conversions, window, tz)

    current_start, current_end = window.current[0], window.current[-1]
    in_range = [c for c in report.conversions if current_start <= c.purchased_at.astimezone(tz).date() <= current_end]

    metric = metric if metric in METRICS else PRIMARY_METRIC
    label, kind = METRICS[metric]
    cards = [
        {"metric": key, "label": key_label, "derived": _derived(series[key], days, key_kind, comparable)}
        for key, (key_label, key_kind) in METRICS.items()
    ]
    counted = [c for c in in_range if _counts(c)]
    commission = sum((c.commission for c in counted), Decimal(0))
    return {
        "is_fresh": not report.conversions,
        "report_complete": report.complete,
        "comparable": comparable,
        "max_days": REPORT_MAX_DAYS,
        "hero_cards": cards,
        "average_commission": commission / len(counted) if counted else None,
        "status_summary": _status_summary(in_range),
        "top_products": _top_products(in_range),
        "referrers": _referrers(in_range),
        "chart": {
            "metric": metric,
            "label": label,
            "chips": [{"key": key, "label": key_label} for key, (key_label, _kind) in METRICS.items()],
            "derived": _derived(series[metric], days, kind, comparable),
            "labels": [d.isoformat() for d in window.current],
        },
        "table": _table(in_range, status=status if status in STATUS_FILTERS else "all", page=page, tz=tz),
    }
