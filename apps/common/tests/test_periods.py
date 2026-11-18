"""The reporting period of the analytics, dashboard and ads pages: a preset
length or the dates someone picked on the calendar."""

from datetime import date

import pytest
from django.http import QueryDict

from apps.common.periods import MAX_CUSTOM_DAYS, parse_period

TODAY = date(2026, 11, 18)


def _parse(query, **kwargs):
    options = {"choices": (7, 30, 90), "default": 30, "today": TODAY, **kwargs}
    return parse_period(QueryDict(query), **options)


def test_a_preset_is_the_last_days_up_to_today():
    period = _parse("range=7")

    assert (period.days, period.start, period.end, period.custom) == (7, date(2026, 11, 12), TODAY, False)
    assert period.end_param is None
    assert period.query == "range=7"


def test_picked_dates_make_a_custom_period_of_those_days():
    period = _parse("range=7&start=2026-10-01&end=2026-10-15")

    assert (period.days, period.start, period.end, period.custom) == (15, date(2026, 10, 1), date(2026, 10, 15), True)
    assert period.end_param == date(2026, 10, 15)
    assert period.query == "start=2026-10-01&end=2026-10-15"


@pytest.mark.parametrize(
    "query",
    [
        "start=2026-10-15&end=2026-10-01",  # reversed
        "start=2026-11-10&end=2026-11-19",  # ends after today
        "start=2026-10-01",  # no end
        "start=nonsense&end=2026-10-01",
        "start=2024-01-01&end=2026-10-01",  # longer than the limit
        "range=45",  # not one of the presets
    ],
)
def test_anything_else_falls_back_to_the_default(query):
    period = _parse(query)

    assert (period.days, period.end, period.custom) == (30, TODAY, False)


def test_the_longest_custom_period_is_accepted():
    period = _parse(f"start=2025-11-19&end={TODAY.isoformat()}")

    assert period.custom and period.days == MAX_CUSTOM_DAYS


def test_dates_before_what_the_source_reports_are_refused():
    period = _parse("start=2026-08-01&end=2026-08-10", min_date=date(2026, 9, 1))

    assert not period.custom


def test_a_filter_with_its_own_prefix_follows_the_page_until_changed():
    page = _parse("start=2026-10-01&end=2026-10-15")

    following = _parse("start=2026-10-01&end=2026-10-15", prefix="table_", allow_all=True, fallback=page)
    everything = _parse("table_range=all", prefix="table_", allow_all=True, fallback=page)
    own = _parse("table_start=2026-09-01&table_end=2026-09-02", prefix="table_", fallback=page)

    assert (following.custom, following.start) == (True, date(2026, 10, 1))
    assert following.query == "table_start=2026-10-01&table_end=2026-10-15"
    assert everything.everything and everything.query == "table_range=all"
    assert own.days == 2 and own.keys == ("table_range", "table_start", "table_end")
