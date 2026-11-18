"""The reporting period a page reads its numbers over: one of its preset lengths
("last 7/30/90 days", ``?range=30``) or a custom start and end picked on the
calendar (``?start=2026-10-01&end=2026-10-15``).

Every period is ``days`` days ending on ``end``, so the code that compares a
period with the one before it (``2 * days`` series, ``derive``) works the same
for both kinds. Parsed by :func:`parse_period`; drawn by the ``period_picker``
tag (``components/_period_picker.html``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlencode

from django.utils import formats
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

MAX_CUSTOM_DAYS = 365


@dataclass(frozen=True)
class Period:
    """``days`` days ending on ``end``.

    ``custom`` when the person picked the dates. ``prefix`` and ``name`` are the
    parameter names it was read with (``table_`` + ``range`` for the analytics
    posts table), so :attr:`query` writes it back the same way. ``everything``
    stands for an "All time" choice where the page offers one. ``today`` and
    ``min_date`` bound the dates the calendar lets someone pick.
    """

    days: int
    end: date
    today: date
    custom: bool = False
    prefix: str = ""
    name: str = "range"
    everything: bool = False
    min_date: date | None = None

    @property
    def start(self) -> date:
        return self.end - timedelta(days=self.days - 1)

    @property
    def end_param(self) -> date | None:
        """The end to hand the services: ``None`` (up to today) for a preset."""
        return self.end if self.custom else None

    @property
    def query(self) -> str:
        """The period as query parameters, for the links and HTMX requests that keep it."""
        if self.custom:
            return urlencode({f"{self.prefix}start": self.start.isoformat(), f"{self.prefix}end": self.end.isoformat()})
        return urlencode({f"{self.prefix}{self.name}": "all" if self.everything else self.days})

    @property
    def keys(self) -> tuple[str, str, str]:
        """The query parameters this period owns."""
        return (f"{self.prefix}{self.name}", f"{self.prefix}start", f"{self.prefix}end")

    @property
    def label(self) -> str:
        """ "last 30 days" or the two dates, for headings and empty states."""
        if self.custom:
            return f"{_short(self.start)} – {_short(self.end)}"
        if self.everything:
            return _("all time")
        return ngettext("last %(days)d day", "last %(days)d days", self.days) % {"days": self.days}


def parse_period(
    params,
    *,
    choices: tuple[int, ...],
    default: int,
    today: date,
    name: str = "range",
    prefix: str = "",
    allow_all: bool = False,
    min_date: date | None = None,
    fallback: Period | None = None,
) -> Period:
    """The period in ``params`` (``request.GET``).

    A custom ``{prefix}start``/``{prefix}end`` wins when both are valid dates,
    in order, no later than ``today``, no earlier than ``min_date`` and at most
    :data:`MAX_CUSTOM_DAYS` long. Otherwise ``{prefix}{name}`` picks a preset
    (``"all"`` when ``allow_all``); anything else falls back to ``fallback``
    (the page's own period, for a filter that follows it) or to ``default``.
    """

    def make(days: int, end: date, *, custom: bool = False, everything: bool = False) -> Period:
        return Period(
            days=days,
            end=end,
            today=today,
            custom=custom,
            prefix=prefix,
            name=name,
            everything=everything,
            min_date=min_date,
        )

    start, end = _date(params.get(f"{prefix}start")), _date(params.get(f"{prefix}end"))
    if start and end and start <= end <= today and (min_date is None or start >= min_date):
        days = (end - start).days + 1
        if days <= MAX_CUSTOM_DAYS:
            return make(days, end, custom=True)
    raw = params.get(f"{prefix}{name}")
    if allow_all and raw == "all":
        return make(default, today, everything=True)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = 0
    if days in choices:
        return make(days, today)
    if fallback is not None:
        return make(fallback.days, fallback.end, custom=fallback.custom, everything=fallback.everything)
    return make(default, today)


def _date(value) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _short(day: date) -> str:
    return formats.date_format(day, "SHORT_DATE_FORMAT")
