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

# Content ideas offered for a date that brings none of its own, by category
# (``SpecialDate.Kind``; a built-in holiday counts as national).
GENERIC_SUGGESTIONS = {
    "commercial": (
        _("Special offer for the date"),
        _("Countdown in Stories"),
        _("Gift or product guide"),
        _("Customer testimonial"),
    ),
    "seasonal": (
        _("Tips for the season"),
        _("Products or services that fit the moment"),
        _("Behind the scenes of the preparation"),
    ),
    "national": (
        _("Message from the brand for the date"),
        _("A fact about the date"),
        _("Behind the scenes of the preparation"),
    ),
    # Not a category: the built-in public holidays, where the shop may close.
    HOLIDAY: (
        _("Message from the brand for the date"),
        _("Opening hours on the holiday"),
        _("A fact about the date"),
    ),
    "international": (
        _("What the date means to your brand"),
        _("A fact about the date"),
        _("Invite your audience to take part"),
    ),
}


def suggestions_for(kind: str) -> list[str]:
    return [str(text) for text in GENERIC_SUGGESTIONS.get(kind, ())]


# (month, day, slug, name, emoji, kind). The slug names the date for good:
# it keys ``BUILT_IN_CONTENT`` and the brands' favorites.
FIXED = (
    (1, 1, "new-year", _("New Year's Day"), "🎉", HOLIDAY),
    (3, 8, "womens-day", _("International Women's Day"), "💜", COMMERCIAL),
    (3, 15, "consumer-day", _("Consumer Day"), "🛍️", COMMERCIAL),
    (4, 21, "tiradentes", _("Tiradentes Day"), "🇧🇷", HOLIDAY),
    (5, 1, "labour-day", _("Labour Day"), "🛠️", HOLIDAY),
    (6, 12, "valentines", _("Valentine's Day"), "❤️", COMMERCIAL),
    (6, 24, "saint-john", _("Saint John's Day (Festa Junina)"), "🌽", COMMERCIAL),
    (9, 7, "independence", _("Independence Day"), "🇧🇷", HOLIDAY),
    (9, 15, "customer-day", _("Customer Day"), "🤝", COMMERCIAL),
    (10, 12, "childrens-day", _("Children's Day"), "🧸", HOLIDAY),
    (10, 15, "teachers-day", _("Teachers' Day"), "🍎", COMMERCIAL),
    (10, 31, "halloween", _("Halloween"), "🎃", COMMERCIAL),
    (11, 2, "all-souls", _("All Souls' Day"), "🕯️", HOLIDAY),
    (11, 15, "republic", _("Proclamation of the Republic"), "🇧🇷", HOLIDAY),
    (11, 20, "black-consciousness", _("Black Consciousness Day"), "✊🏿", HOLIDAY),
    (12, 25, "christmas", _("Christmas"), "🎄", HOLIDAY),
    (12, 31, "new-years-eve", _("New Year's Eve"), "🥂", COMMERCIAL),
)

# What the special-dates tab says about each built-in date: slug →
# (description, tags as one comma-separated string). Both are translated.
BUILT_IN_CONTENT = {
    "new-year": (
        _("The first day of the year: a moment for fresh starts, goals and good wishes to your audience."),
        _("newyear, goals, freshstart"),
    ),
    "womens-day": (
        _(
            "A date to recognise women and their achievements. Speak with respect, and show the women behind your brand."
        ),
        _("womensday, women, respect"),
    ),
    "consumer-day": (
        _("One of the strongest sales dates of the first half of the year, with offers across retail and services."),
        _("consumerday, offers, discount"),
    ),
    "tiradentes": (
        _("National holiday in memory of Tiradentes. Many businesses close or change their hours."),
        _("tiradentes, holiday"),
    ),
    "labour-day": (
        _("National holiday that honours workers. A good moment to thank your team and show who makes the brand."),
        _("labourday, team, holiday"),
    ),
    "valentines": (
        _("Brazil's day for couples, on 12 June. Gifts, experiences and dinners sell well in the weeks before."),
        _("valentinesday, love, gifts"),
    ),
    "saint-john": (
        _("The heart of the June festivals: bonfires, typical food and country-style parties across Brazil."),
        _("festajunina, saintjohn, typicalfood"),
    ),
    "independence": (
        _("National holiday for Brazil's independence, on 7 September. Green and yellow set the tone."),
        _("independence, brazil, holiday"),
    ),
    "customer-day": (
        _("A date to thank the people who choose your brand, with a message, a benefit or a special condition."),
        _("customerday, thankyou, loyalty"),
    ),
    "childrens-day": (
        _("One of the main gift dates of the year, on the same day as the holiday of Our Lady of Aparecida."),
        _("childrensday, kids, gifts"),
    ),
    "teachers-day": (
        _("A date to thank teachers and everyone who teaches. Works well with stories about who taught you."),
        _("teachersday, education, thankyou"),
    ),
    "halloween": (
        _("Costumes, sweets and playful scares on 31 October. Themed content and offers get attention."),
        _("halloween, costume, sweets"),
    ),
    "all-souls": (
        _("A day of remembrance and a national holiday. The tone is quiet and respectful; avoid sales messages."),
        _("allsouls, remembrance, holiday"),
    ),
    "republic": (
        _("National holiday for the Proclamation of the Republic, on 15 November."),
        _("republic, brazil, holiday"),
    ),
    "black-consciousness": (
        _("A date of respect, equality and appreciation of Afro-Brazilian culture, and a national holiday."),
        _("blackconsciousness, equality, culture"),
    ),
    "christmas": (
        _("A time of togetherness and the biggest gift season of the year. Campaigns start weeks before."),
        _("christmas, gifts, family"),
    ),
    "new-years-eve": (
        _("The last night of the year: a look back, thanks to customers and wishes for the year ahead."),
        _("newyearseve, lookback, celebration"),
    ),
    "carnival": (
        _("Brazil's biggest street party. Many businesses change their hours, and colourful content travels far."),
        _("carnival, party, holiday"),
    ),
    "good-friday": (
        _("A religious holiday before Easter. The tone is calm; many businesses close for the day."),
        _("goodfriday, easter, holiday"),
    ),
    "easter": (
        _("Chocolate, family lunches and renewal. Sales of gifts and food grow in the weeks before."),
        _("easter, chocolate, family"),
    ),
    "mothers-day": (
        _("The second biggest retail date of the year in Brazil, on the second Sunday of May."),
        _("mothersday, mother, gifts"),
    ),
    "corpus-christi": (
        _("A religious date sixty days after Easter, a day off in much of the country."),
        _("corpuschristi, holiday"),
    ),
    "fathers-day": (
        _("On the second Sunday of August: gifts, tributes and stories about fathers."),
        _("fathersday, father, gifts"),
    ),
    "black-friday": (
        _("One of the biggest chances of the year to sell more, with special conditions, discounts and campaigns."),
        _("blackfriday, sales, discount, promotion"),
    ),
}


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
        (easter_day - timedelta(days=47), "carnival", _("Carnival"), "🎭", HOLIDAY),
        (easter_day - timedelta(days=2), "good-friday", _("Good Friday"), "✝️", HOLIDAY),
        (easter_day, "easter", _("Easter"), "🐣", COMMERCIAL),
        (nth_weekday(year, 5, 6, 2), "mothers-day", _("Mother's Day"), "💐", COMMERCIAL),
        (easter_day + timedelta(days=60), "corpus-christi", _("Corpus Christi"), "⛪", HOLIDAY),
        (nth_weekday(year, 8, 6, 2), "fathers-day", _("Father's Day"), "👔", COMMERCIAL),
        (nth_weekday(year, 11, 3, 4) + timedelta(days=1), "black-friday", _("Black Friday"), "🏷️", COMMERCIAL),
    )


@lru_cache(maxsize=16)
def _year(year: int) -> tuple:
    entries = [(date(year, m, d), slug, name, emoji, kind) for m, d, slug, name, emoji, kind in FIXED]
    entries.extend(_movable(year))
    return tuple(sorted(entries, key=lambda e: e[0]))


def _tag_list(text) -> list[str]:
    return [tag.strip() for tag in str(text).split(",") if tag.strip()]


def _managed_between(start_date, end_date, workspace) -> list[dict]:
    """The operator's dates in the range: the global ones, plus those of the
    segments ``workspace`` follows. A day that does not exist in a year
    (29 February) is skipped that year."""
    from django.db.models import Q

    from .models import SpecialDate

    wanted = Q(segment__isnull=True)
    if workspace is not None:
        wanted |= Q(segment__is_active=True, segment__workspace_choices__workspace=workspace)
    result = []
    rows = SpecialDate.objects.filter(wanted, is_active=True).select_related("segment").distinct()
    for row in rows:
        for year in range(start_date.year, end_date.year + 1):
            try:
                day = date(year, row.month, row.day)
            except ValueError:
                continue
            if start_date <= day <= end_date:
                result.append(
                    {
                        "date": day,
                        "name": row.name,
                        "emoji": row.emoji,
                        "type": COMMERCIAL,
                        "segment": row.segment.name if row.segment else "",
                        "built_in": False,
                        "key": f"d:{row.id}",
                        "image": row.image.url if row.image else "",
                        "kind": row.kind,
                        "description": row.description,
                        "suggestions": row.suggestion_list or suggestions_for(row.kind),
                        "tags": row.tag_list,
                    }
                )
    return result


def special_dates_between(start_date, end_date, workspace=None) -> list[dict]:
    """``[{"date", "name", "emoji", "type", "segment", "built_in", "key",
    "image", "kind", "description", "suggestions", "tags"}]`` from
    ``start_date`` to ``end_date`` inclusive, by date.

    Global dates (the built-in ones and those the operator added without a
    segment) always come; with ``workspace``, so do the dates of the segments
    it follows, each carrying its segment's name (``""`` for a global one).
    ``built_in`` tells the dates in this module from the operator's.
    ``type`` is what the calendar grid styles by (holiday / commercial);
    ``kind`` is the category of the special-dates tab (``SpecialDate.Kind``).
    """
    result = []
    for year in range(start_date.year, end_date.year + 1):
        for day, slug, name, emoji, kind in _year(year):
            if start_date <= day <= end_date:
                description, tags = BUILT_IN_CONTENT.get(slug, ("", ""))
                result.append(
                    {
                        "date": day,
                        "name": str(name),
                        "emoji": emoji,
                        "type": kind,
                        "segment": "",
                        "built_in": True,
                        "key": f"b:{slug}",
                        "image": "",
                        "kind": "national" if kind == HOLIDAY else "commercial",
                        "description": str(description),
                        "suggestions": suggestions_for(kind),
                        "tags": _tag_list(tags),
                    }
                )
    result.extend(_managed_between(start_date, end_date, workspace))
    return sorted(result, key=lambda entry: entry["date"])


def get_holidays_for_range(start_date, end_date, workspace=None):
    """Special dates in the range, keyed by ISO date (several can share a day)."""
    result: dict[str, list[dict]] = {}
    for entry in special_dates_between(start_date, end_date, workspace):
        result.setdefault(entry["date"].isoformat(), []).append(entry)
    return result
