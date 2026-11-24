"""Markdown the team writes for a page of the product, rendered to safe HTML.

For texts shown on screen (the help centre's tutorials and answers). E-mail
has its own renderer, ``apps.email_center.markdown_mail``, which inlines the
mail's styles. Whatever the Markdown holds, the result keeps only the tags
below (raw HTML, scripts and event attributes are dropped by nh3), and links
open in a new tab without handing over the page.

Style the result with the ``rich-text`` class (theme styles.css).
"""

from __future__ import annotations

import markdown  # type: ignore[import-untyped]
import nh3
from django.utils.safestring import SafeString, mark_safe

ALLOWED_TAGS = {
    "p",
    "br",
    "h2",
    "h3",
    "h4",
    "strong",
    "em",
    "ul",
    "ol",
    "li",
    "a",
    "code",
    "pre",
    "blockquote",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}


def render_markdown(source: str) -> SafeString:
    """``source`` as sanitised HTML; an empty text gives an empty string."""
    if not (source or "").strip():
        return mark_safe("")
    html = markdown.markdown(source, extensions=["extra", "sane_lists"])
    cleaned = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes={"a": {"href", "title"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )
    return mark_safe(cleaned)  # noqa: S308 - cleaned by nh3 to the tags above
