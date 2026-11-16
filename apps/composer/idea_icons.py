"""The icons a brand can give an idea: line icons in the app's own stroke
(24×24, drawn with ``stroke="currentColor"``), so they take the colour of the
column the card is in, or the colour picked for the idea.

``ICONS`` maps a slug to its name and the inner markup of the ``<svg>``. The
markup is written here, never taken from a user, which is what lets the
templates print it unescaped. An idea stores only the slug (``Idea.icon``)
and, optionally, a tone (``Idea.icon_tone``). The template categories are
drawn from the same library.
"""

from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

#: Shown when an idea has no icon, or one that is no longer in the library.
DEFAULT_ICON = "lightbulb"

ICONS = {
    "lightbulb": (
        _("Idea"),
        '<path d="M9 18h6M10 22h4M12 2a7 7 0 00-4 12.7c.6.5 1 1.2 1 2V17h6v-.3c0-.8.4-1.5 1-2A7 7 0 0012 2z"/>',
    ),
    "target": (
        _("Goal"),
        '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    ),
    "megaphone": (_("Announcement"), '<path d="M3 11l18-5v12L3 14v-3zM11.6 16.8a3 3 0 11-5.8-1.6"/>'),
    "shopping-bag": (
        _("Sale"),
        '<path d="M6 2L3 6v14a2 2 0 002 2h14a2 2 0 002-2V6l-3-4zM3 6h18M16 10a4 4 0 01-8 0"/>',
    ),
    "tag": (
        _("Offer"),
        '<path d="M20.59 13.41l-7.17 7.17a2 2 0 01-2.83 0L2 12V2h10l8.59 8.59a2 2 0 010 2.82zM7 7h.01"/>',
    ),
    "percent": (
        _("Discount"),
        '<path d="M19 5L5 19"/><circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/>',
    ),
    "gift": (
        _("Gift"),
        '<path d="M20 12v10H4V12M2 7h20v5H2zM12 22V7M12 7H7.5a2.5 2.5 0 010-5C11 2 12 7 12 7z'
        'M12 7h4.5a2.5 2.5 0 000-5C13 2 12 7 12 7z"/>',
    ),
    "calendar": (
        _("Date"),
        '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    ),
    "clock": (_("Deadline"), '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>'),
    "camera": (
        _("Photo"),
        '<path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/>'
        '<circle cx="12" cy="13" r="4"/>',
    ),
    "video": (_("Video"), '<path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2"/>'),
    "image": (
        _("Image"),
        '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/>'
        '<path d="M21 15l-5-5L5 21"/>',
    ),
    "pencil": (_("Text"), '<path d="M12 20h9M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4 12.5-12.5z"/>'),
    "book": (
        _("Educational"),
        '<path d="M4 19.5A2.5 2.5 0 016.5 17H20M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z"/>',
    ),
    "help": (
        _("Question"),
        '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3M12 17h.01"/>',
    ),
    "message": (_("Conversation"), '<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>'),
    "users": (
        _("People"),
        '<path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/>'
        '<path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/>',
    ),
    "heart": (
        _("Engagement"),
        '<path d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06'
        'L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z"/>',
    ),
    "smile": (
        _("Humour"),
        '<circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2M9 9h.01M15 9h.01"/>',
    ),
    "star": (
        _("Highlight"),
        '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>',
    ),
    "flag": (_("Flag"), '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1zM4 22v-7"/>'),
    "award": (_("Achievement"), '<circle cx="12" cy="8" r="7"/><path d="M8.21 13.89L7 23l5-3 5 3-1.21-9.12"/>'),
    "zap": (_("Quick tip"), '<path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>'),
    "flame": (
        _("Trend"),
        '<path d="M8.5 14.5A2.5 2.5 0 0011 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5'
        ' 2 1.6 3 3.5 3 5.5a7 7 0 11-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 002.5 2.5z"/>',
    ),
    "trending-up": (_("Growth"), '<path d="M23 6l-9.5 9.5-5-5L1 18M17 6h6v6"/>'),
    "chart": (_("Results"), '<path d="M18 20V10M12 20V4M6 20v-6"/>'),
    "map-pin": (
        _("Place"),
        '<path d="M21 10c0 7-9 13-9 13S3 17 3 10a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/>',
    ),
    "globe": (
        _("World"),
        '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10'
        ' 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/>',
    ),
    "coffee": (
        _("Behind the scenes"),
        '<path d="M18 8h1a4 4 0 010 8h-1M2 8h16v9a4 4 0 01-4 4H6a4 4 0 01-4-4V8zM6 1v3M10 1v3M14 1v3"/>',
    ),
    # The icons of the template categories (see ``CATEGORY_STYLE``).
    "grid": (
        _("All"),
        '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>'
        '<rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    ),
    "scroll": (
        _("Case Study"),
        '<path d="M15 12H10M15 8H10M19 17V5a2 2 0 00-2-2H4M4 3a2 2 0 00-2 2v2a1 1 0 001 1h3m2 13a2 2 0 01-2-2v-1'
        'a1 1 0 011-1h10a1 1 0 011 1v1a2 2 0 01-2 2H8z"/>',
    ),
    "book-open": (
        _("Story"),
        '<path d="M12 7V21M12 7C12 5.9 11.6 4.9 10.8 4.2 10.1 3.4 9.1 3 8 3H3a1 1 0 00-1 1v13a1 1 0 001 1h6'
        "c1.6 0 3 1.3 3 3M12 7c0-1.1.4-2.1 1.2-2.8C13.9 3.4 14.9 3 16 3h5a1 1 0 011 1v13a1 1 0 01-1 1h-6"
        'c-1.6 0-3 1.3-3 3"/>',
    ),
    "tools": (
        _("How-to"),
        '<path d="M14.5 12.5L6.6 20.4a2 2 0 01-2.8 0 2 2 0 010-2.8l7.9-7.9M15.7 4.3a1.4 1.4 0 012 0l2 2'
        'a1.4 1.4 0 010 2l-4.6 4.6-4-4 4.6-4.6z"/>',
    ),
    "quote": (
        _("Opinion"),
        '<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/><path d="M8 12h2v-2H8zM14 12h2v-2h-2z"/>',
    ),
    "checklist": (
        _("List"),
        '<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M12 11h4M12 16h4M8 11h.01M8 16h.01"/>'
        '<path d="M9 6h6"/>',
    ),
    "backstage": (
        _("Behind the Scenes"),
        '<path d="M3 11l7-5 4 4 7-5M3 11v8a2 2 0 002 2h14a2 2 0 002-2v-8"/>',
    ),
}

#: The colours an icon can take (`.icon-tone-*` in styles.css, from the
#: semantic tokens). An idea with none takes the colour of its column.
TONES = ("brand", "warning", "success", "info", "error")

#: How each template category is drawn: (icon, tone). ``all`` is the tile
#: that shows every category.
CATEGORY_STYLE = {
    "all": ("grid", "brand"),
    "tip": ("lightbulb", "warning"),
    "case-study": ("scroll", "info"),
    "story": ("book-open", "error"),
    "how-to": ("tools", "success"),
    "question": ("help", "brand"),
    "opinion": ("quote", "info"),
    "list": ("checklist", "success"),
    "behind-the-scenes": ("backstage", "warning"),
}


def category_style(category) -> tuple[str, str]:
    """``(icon, tone)`` of a template category; the default icon on the
    brand's colour for one that is not known."""
    return CATEGORY_STYLE.get(category, (DEFAULT_ICON, "brand"))


def clean_tone(value) -> str:
    """The tone if it is one an icon can take, else nothing."""
    tone = (value or "").strip()
    return tone if tone in TONES else ""


def clean_icon(value) -> str:
    """The slug if it names an icon of the library, else nothing."""
    slug = (value or "").strip()
    return slug if slug in ICONS else ""


def icon_markup(slug) -> str:
    """The inner markup of the icon's ``<svg>``; the default icon's for an
    unknown or empty slug."""
    return mark_safe(ICONS.get(slug, ICONS[DEFAULT_ICON])[1])  # noqa: S308 - markup from this module only


def icon_library() -> list[dict]:
    """The library for the page's picker: ``[{"slug", "label", "markup"}]``."""
    return [{"slug": slug, "label": str(label), "markup": markup} for slug, (label, markup) in ICONS.items()]
