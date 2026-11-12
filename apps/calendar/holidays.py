"""Brazilian holidays and commercial dates shown on the calendar and on the idea board.

Fixed dates repeat on the same day every year; the rest move (Carnival and
Corpus Christi follow Easter, Mother's Day is the second Sunday of May, Black
Friday the day after the fourth Thursday of November) and are computed per year.
Names are translatable source strings, so they render in the user's language.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from django.utils.translation import gettext_lazy as _

HOLIDAY = "holiday"
COMMERCIAL = "commercial"

# (month, day, name, emoji, kind)
FIXED = (
    (1, 1, _("New Year's Day"), "🎉", HOLIDAY),
    (3, 8, _("International Women's Day"), "💜", COMMERCIAL),
    (3, 15, _("Consumer Day"), "🛍️", COMMERCIAL),
    (4, 21, _("Tiradentes Day"), "🇧🇷", HOLIDAY),
    (5, 1, _("Labour Day"), "🛠️", HOLIDAY),
    (6, 12, _("Valentine's Day"), "❤️", COMMERCIAL),
    (6, 24, _("Saint John's Day (Festa Junina)"), "🌽", COMMERCIAL),
    (9, 7, _("Independence Day"), "🇧🇷", HOLIDAY),
    (9, 15, _("Customer Day"), "🤝", COMMERCIAL),
    (10, 12, _("Children's Day"), "🧸", HOLIDAY),
    (10, 15, _("Teachers' Day"), "🍎", COMMERCIAL),
    (10, 31, _("Halloween"), "🎃", COMMERCIAL),
    (11, 2, _("All Souls' Day"), "🕯️", HOLIDAY),
    (11, 15, _("Proclamation of the Republic"), "🇧🇷", HOLIDAY),
    (11, 20, _("Black Consciousness Day"), "✊🏿", HOLIDAY),
    (12, 25, _("Christmas"), "🎄", HOLIDAY),
    (12, 31, _("New Year's Eve"), "🥂", COMMERCIAL),
)


def easter(year: int) -> date:
    """Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Monday=0) of a month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _movable(year: int):
    easter_day = easter(year)
    return (
        (easter_day - timedelta(days=47), _("Carnival"), "🎭", HOLIDAY),
        (easter_day - timedelta(days=2), _("Good Friday"), "✝️", HOLIDAY),
        (easter_day, _("Easter"), "🐣", COMMERCIAL),
        (nth_weekday(year, 5, 6, 2), _("Mother's Day"), "💐", COMMERCIAL),
        (easter_day + timedelta(days=60), _("Corpus Christi"), "⛪", HOLIDAY),
        (nth_weekday(year, 8, 6, 2), _("Father's Day"), "👔", COMMERCIAL),
        (nth_weekday(year, 11, 3, 4) + timedelta(days=1), _("Black Friday"), "🏷️", COMMERCIAL),
    )


@lru_cache(maxsize=16)
def _year(year: int) -> tuple:
    entries = [(date(year, m, d), name, emoji, kind) for m, d, name, emoji, kind in FIXED]
    entries.extend(_movable(year))
    return tuple(sorted(entries, key=lambda e: e[0]))


def special_dates_between(start_date, end_date) -> list[dict]:
    """``[{"date", "name", "emoji", "type"}]`` from ``start_date`` to ``end_date`` inclusive, by date."""
    result = []
    for year in range(start_date.year, end_date.year + 1):
        for day, name, emoji, kind in _year(year):
            if start_date <= day <= end_date:
                result.append({"date": day, "name": str(name), "emoji": emoji, "type": kind})
    return result


def get_holidays_for_range(start_date, end_date):
    """Special dates in the range, keyed by ISO date (several can share a day)."""
    result: dict[str, list[dict]] = {}
    for entry in special_dates_between(start_date, end_date):
        result.setdefault(entry["date"].isoformat(), []).append(entry)
    return result
