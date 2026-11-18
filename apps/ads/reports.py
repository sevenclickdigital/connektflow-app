"""Meta Ads on the Reports page: the ad account's results over a range.

Shaped like the analytics page's own payloads (``apps.analytics``), as the
Shopee Affiliates report is: KPI cards and the hero chart are
:class:`~apps.analytics.derive.DerivedMetric` built by the same ``derive``, so
the shared card, chart and delta partials render them.

Read live from Meta: one daily ``/insights`` call over twice the range (the
previous period is the comparison) and one per-campaign call for the range.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.analytics.derive import DerivedMetric, derive

from . import services
from .models import AdsConnection

TOP_CAMPAIGNS = 8

# key → (label, kind). A currency kind carries the ad account's currency.
METRICS = {
    "spend": (_("Amount spent"), "currency"),
    "impressions": (_("Impressions"), "count"),
    "link_clicks": (_("Link clicks"), "count"),
    "results": (_("Results"), "count"),
}
PRIMARY_METRIC = "spend"


def _kind(kind: str, connection: AdsConnection) -> str:
    return f"currency:{connection.currency}" if kind == "currency" and connection.currency else kind


def _series(rows, dates: list[date]) -> dict[str, list[float]]:
    """Each metric per day over ``dates``; a day Meta did not report spent nothing."""
    by_day = {row.date_start: row for row in rows}
    series: dict[str, list[float]] = {key: [] for key in METRICS}
    for day in dates:
        row = by_day.get(day.isoformat())
        series["spend"].append(float(row.spend) if row else 0.0)
        series["impressions"].append(float(row.impressions) if row else 0.0)
        series["link_clicks"].append(float(row.link_clicks) if row else 0.0)
        series["results"].append(float(row.purchases + row.leads) if row else 0.0)
    return series


def build_report(connection: AdsConnection, *, days: int, metric: str | None = None) -> dict:
    """Everything the Meta Ads report shows for the last ``days`` days.

    Raises :class:`services.AdsError` when Meta refuses.
    """
    today = timezone.localdate()
    dates = [today - timedelta(days=2 * days - 1 - i) for i in range(2 * days)]
    rows = services.account_insights(connection, since=dates[0].isoformat(), until=today.isoformat(), daily=True)
    series = _series(rows, dates)

    def derived(key: str) -> DerivedMetric:
        return derive(series[key], days, _kind(METRICS[key][1], connection))

    metric = metric if metric in METRICS else PRIMARY_METRIC
    campaigns = services.list_campaigns(connection, days=days)
    spent = [c for c in campaigns if c.insights.spend or c.insights.impressions]
    spent.sort(key=lambda c: c.insights.spend, reverse=True)
    return {
        "is_fresh": not any(any(values) for values in series.values()),
        "currency": connection.currency,
        "hero_cards": [
            {"metric": key, "label": label, "derived": derived(key)} for key, (label, _k) in METRICS.items()
        ],
        "chart": {
            "metric": metric,
            "label": METRICS[metric][0],
            "chips": [{"key": key, "label": label} for key, (label, _k) in METRICS.items()],
            "derived": derived(metric),
            "labels": [d.isoformat() for d in dates[-days:]],
        },
        "top_campaigns": spent[:TOP_CAMPAIGNS],
        "campaign_count": len(campaigns),
        "active_campaigns": sum(1 for c in campaigns if c.is_active),
    }
