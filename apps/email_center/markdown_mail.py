"""Markdown e-mail: what the team writes in the administrative area, turned into our layout.

The syntax is plain Markdown plus three conventions, in the spirit of
Laravel's Markdown mail:

- the first line ``# Title`` is the heading, shown beside the mascot;
- a paragraph holding only ``[button: Label](url)`` (or ``botão:``) becomes
  the layout's call-to-action button;
- ``> text`` becomes the lilac panel the layout uses for highlighted content,
  and ``> <small>…</small>`` the compact box of the security notices;
- ``<small>…</small>`` alone in a paragraph is a note in small grey letters,
  and ``` `…` ``` alone is the large box of sign-in codes;
- a Markdown table is the withdrawal card (its header row on top);
- a ``<small>…</small>`` paragraph right under a button is the "if the button
  doesn't work, copy this link" line, in that line's small print. The button
  adds nothing of its own: every word under it is the team's to change.

Each block is emitted with the markup the default templates use, so an
edited e-mail looks like the one it replaces.

``{{ name }}`` puts in the value of a variable. Values never pass through the
Markdown parser or the sanitizer as Markdown: each one is swapped for an
inert token first and HTML-escaped when the token is replaced, so a person's
name cannot add formatting, markup or a link. A value placed in a link must
be an ``http``, ``https`` or ``mailto`` address, else the link goes nowhere.
The HTML the parser produces is cleaned to a short list of tags.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

import markdown  # type: ignore[import-untyped]
import nh3
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_TOKEN = "cfvar{}z"
_TOKEN_RE = re.compile(r"cfvar(\d+)z")
_BUTTON_PREFIX = r"(?:bot[aã]o|button)\s*:\s*"
_BUTTON_RE = re.compile(r"<p[^>]*>\s*<a ([^>]*)>\s*" + _BUTTON_PREFIX + r"(.*?)</a>\s*</p>", re.I | re.S)
# A title on a button's link (``[button: X](url "…")``) changes nothing; the plain text drops it.
_TEXT_BUTTON_TITLE_RE = re.compile(r'(\]\([^)\s]+)\s+"[^"]*"\)')
_AFTER_BUTTON = "<!--cf-after-button-->"
_TEXT_BUTTON_RE = re.compile(r"\[\s*" + _BUTTON_PREFIX + r"([^\]]+)\]\(([^)\s]+)\)", re.I)
_TEXT_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_SAFE_LINK_RE = re.compile(r"^(https?://|mailto:)", re.I)

ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "b",
    "em",
    "i",
    "a",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "blockquote",
    "hr",
    "code",
    "small",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}

_TEXT = "font-size:15px;line-height:24px;color:#334155;"
_STYLES = {
    "p": "margin:0 0 16px;" + _TEXT,
    "ul": "margin:0 0 16px;padding:0 0 0 20px;" + _TEXT,
    "ol": "margin:0 0 16px;padding:0 0 0 20px;" + _TEXT,
    "li": "margin:0 0 4px;",
    "h1": "margin:24px 0 12px;font-size:18px;line-height:26px;font-weight:700;color:#0F172A;",
    "h2": "margin:8px 0 12px;font-size:16px;line-height:24px;font-weight:700;color:#0F172A;",
    "h3": "margin:20px 0 8px;font-size:16px;line-height:24px;font-weight:700;color:#0F172A;",
    "strong": "color:#0F172A;",
    "b": "color:#0F172A;",
    "a": "color:#7C3AED;text-decoration:underline;",
    "hr": "border:0;border-top:1px solid #E5E7EB;margin:24px 0;",
}


# The layout's blocks, with the exact markup the default templates use.
_PANEL_MARGIN = "margin:0 0 16px;"
_PANEL = (
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="{margin}">'
    '<tr><td style="padding:16px 18px;background-color:#F5F3FF;border-radius:8px;font-size:14px;'
    'line-height:24px;color:#475569;">{inner}</td></tr></table>'
)
# The compact box of the security notices ("Where the change came from"): ``> <small>…</small>``.
_DETAILS = (
    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="{margin}">'
    '<tr><td style="padding:14px 18px;background-color:#F5F3FF;border-radius:8px;font-size:13px;'
    'line-height:21px;color:#475569;">{inner}</td></tr></table>'
)
_NOTE = '<p style="margin:0 0 16px;font-size:13px;line-height:20px;color:#64748B;">{inner}</p>'
# The note right under a button: the "copy this link" line of emails/_button.html.
_FALLBACK = (
    '<p class="cf-btn-fallback" style="margin:18px 0 0;font-size:12px;line-height:18px;color:#64748B;">{inner}</p>'
)
_FALLBACK_LINK = 'style="color:#7C3AED;word-break:break-all;text-decoration:underline;"'
_CODE = (
    '<p style="margin:8px 0 24px;padding:18px;background-color:#F5F3FF;border-radius:8px;text-align:center;'
    "font-size:28px;line-height:34px;font-weight:700;letter-spacing:6px;color:#0F172A;"
    'font-family:SFMono-Regular,Menlo,Consolas,monospace;">{inner}</p>'
)
# A Markdown table: the header row on top, highlighted, then one label/value per row
# (the withdrawal card of the affiliate e-mails).
_CARD = (
    '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="margin:0 0 24px;'
    'border:1px solid #E9E5FB;border-radius:10px;border-collapse:separate;">{rows}</table>'
)
_CARD_TOP = (
    '<tr><td colspan="2" style="padding:18px 20px 14px;background-color:#F5F3FF;border-radius:10px 10px 0 0;">'
    '<span style="display:block;font-size:12px;line-height:18px;color:#6D28D9;font-weight:600;'
    'text-transform:uppercase;letter-spacing:0.04em;">{label}</span>'
    '<span style="display:block;font-size:28px;line-height:36px;font-weight:700;color:#0F172A;">{value}</span></td></tr>'
)
_CARD_ROW = (
    '<tr><td style="padding:12px 20px;border-top:1px solid #E9E5FB;font-size:14px;line-height:20px;color:#64748B;">'
    "{label}</td>"
    '<td align="right" style="padding:12px 20px;border-top:1px solid #E9E5FB;font-size:14px;line-height:20px;'
    'color:#0F172A;font-weight:600;word-break:break-all;">{value}</td></tr>'
)
_SPACER = '<div style="height:24px;line-height:24px;font-size:0;">&nbsp;</div>'


@dataclass(frozen=True)
class RenderedEmail:
    heading: str  # HTML-escaped, safe to place in the layout
    html: str  # the body, sanitized and styled for e-mail clients
    text: str  # the plain-text alternative


def variables_used(*texts: str) -> set[str]:
    """Every ``{{ name }}`` in ``texts``."""
    return {match.group(1) for text in texts for match in VARIABLE_RE.finditer(text or "")}


def substitute_text(text: str, values: dict[str, str]) -> str:
    """``text`` with each known ``{{ name }}`` replaced by its raw value (subjects, plain text)."""
    return VARIABLE_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), text or "")


def _split_heading(source: str) -> tuple[str, str]:
    lines = source.replace("\r\n", "\n").split("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if line.startswith("# "):
            return line[2:].strip(), "\n".join(lines[index + 1 :])
        break
    return "", "\n".join(lines)


def _plain_text(body: str, values: dict[str, str]) -> str:
    text = substitute_text(body, values)
    text = _TEXT_BUTTON_TITLE_RE.sub(r"\1)", text)
    text = _TEXT_BUTTON_RE.sub(lambda m: f"{m.group(1).strip()}: {m.group(2)}", text)
    text = re.sub(r"</?small>", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # A link whose text is its own address ("copy this link") is written once.
    text = _TEXT_LINK_RE.sub(lambda m: m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    return re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text).strip()


def _style(fragment: str) -> str:
    for tag, style in _STYLES.items():

        def add_style(match: re.Match, tag: str = tag, style: str = style) -> str:
            return f'<{tag}{match.group(1) or ""} style="{style}">'

        fragment = re.sub(rf"<{tag}(\s[^>]*)?>", add_style, fragment)
    return fragment


def _button(match: re.Match) -> str:
    attrs = match.group(1)
    href = re.search(r'href="([^"]*)"', attrs)
    # Both parts are already escaped by the sanitizer; marked safe so the
    # button template does not escape them a second time.
    button = render_to_string(
        "emails/_button.html",
        {
            "url": mark_safe(href.group(1) if href else ""),
            "label": mark_safe(match.group(2)),
            "no_fallback": True,
        },
    )
    return button + _AFTER_BUTTON


def _fallback(match: re.Match) -> str:
    inner = re.sub(r'<a ([^>]*?)style="[^"]*"', r"<a \1" + _FALLBACK_LINK, match.group(1))
    return _FALLBACK.format(inner=inner) + _AFTER_BUTTON


def _panel(match: re.Match) -> str:
    inner = re.sub(r"<p[^>]*>", "", match.group(1)).replace("</p>", "<br><br>")
    inner = re.sub(r"(\s*<br>\s*)+$", "", inner.strip())
    compact = re.fullmatch(r"<small>(.*)</small>", inner, flags=re.S)
    if compact:
        return _DETAILS.format(margin=_PANEL_MARGIN, inner=compact.group(1).strip())
    return _PANEL.format(margin=_PANEL_MARGIN, inner=inner)


def _cells(row: str, tag: str) -> list[str]:
    return [cell.strip() for cell in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", row, flags=re.S)]


def _card(match: re.Match) -> str:
    table = match.group(0)
    head = re.search(r"<thead>(.*?)</thead>", table, flags=re.S)
    header = _cells(head.group(1), "th") if head else []
    rows = [
        _cells(row, "td")
        for row in re.findall(r"<tr>(.*?)</tr>", re.sub(r"<thead>.*?</thead>", "", table, flags=re.S), flags=re.S)
    ]
    html_rows = ""
    if header:
        html_rows += _CARD_TOP.format(label=header[0], value=header[1] if len(header) > 1 else "")
    for cells in rows:
        if cells:
            html_rows += _CARD_ROW.format(label=cells[0], value=cells[1] if len(cells) > 1 else "")
    return _CARD.format(rows=html_rows)


def _blocks(fragment: str) -> str:
    """Swap the Markdown conventions for the layout's own blocks, spaced like the default templates."""
    fragment = re.sub(r"<table>.*?</table>", _card, fragment, flags=re.S)
    fragment = re.sub(r"<blockquote>(.*?)</blockquote>", _panel, fragment, flags=re.S)
    fragment = re.sub(
        r"<p[^>]*>\s*<code>(.*?)</code>\s*</p>", lambda m: _CODE.format(inner=m.group(1)), fragment, flags=re.S
    )
    fragment = re.sub(
        r"<p[^>]*>\s*<small>(.*?)</small>\s*</p>", lambda m: _NOTE.format(inner=m.group(1)), fragment, flags=re.S
    )
    # The last item of a list closes it without a gap, as in the default templates.
    fragment = re.sub(
        r'<li style="margin:0 0 4px;">((?:(?!<li\b).)*?</li>\s*</[uo]l>)',
        r'<li style="margin:0;">\1',
        fragment,
        flags=re.S,
    )
    fragment = _BUTTON_RE.sub(_button, fragment)
    fragment = re.sub(
        re.escape(_AFTER_BUTTON) + r"\s*" + re.escape(_NOTE).replace(r"\{inner\}", "(.*?)"),
        _fallback,
        fragment,
        flags=re.S,
    )
    # A box right before a button sits close to it, as in the welcome e-mail.
    fragment = re.sub(
        r'(style=")' + re.escape(_PANEL_MARGIN) + r'(">(?:(?!</table>).)*</table>\s*<table[^>]*class="cf-btn")',
        r"\1margin:0 0 4px;\2",
        fragment,
        flags=re.S,
    )
    fragment = fragment.rstrip()
    if fragment.endswith(_AFTER_BUTTON):
        fragment = fragment[: -len(_AFTER_BUTTON)]
    fragment = fragment.replace(_AFTER_BUTTON, _SPACER)
    # The last block sits on the layout's own bottom padding, like the templates' last paragraph.
    last = fragment.rfind('style="margin:0 0 16px;')
    if last != -1 and not re.search(r"<(p|table|ul|ol|div)\b", fragment[last + 8 :]):
        fragment = fragment[:last] + 'style="margin:0;' + fragment[last + len('style="margin:0 0 16px;') :]
    return fragment


def render(source: str, values: dict[str, str]) -> RenderedEmail:
    """Turn Markdown ``source`` into the heading, HTML body and plain text of an e-mail."""
    values = {name: "" if value is None else str(value) for name, value in values.items()}
    heading_source, body = _split_heading(source)

    tokens: list[str] = []

    def tokenize(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            return match.group(0)
        tokens.append(values[name])
        return _TOKEN.format(len(tokens) - 1)

    tokenized = VARIABLE_RE.sub(tokenize, body)
    fragment = markdown.markdown(tokenized, extensions=["sane_lists", "nl2br", "tables"])
    fragment = nh3.clean(
        fragment,
        tags=ALLOWED_TAGS,
        attributes={"a": {"href", "title"}},
        url_schemes={"http", "https", "mailto"},
        link_rel=None,
    )
    # Styled before the layout's blocks go in, so they keep their own styles.
    fragment = _style(fragment)
    fragment = _blocks(fragment)

    def link_value(match: re.Match) -> str:
        href = _TOKEN_RE.sub(lambda m: tokens[int(m.group(1))], html.unescape(match.group(1)))
        return f'href="{html.escape(href) if _SAFE_LINK_RE.match(href) else "#"}"'

    fragment = re.sub(r'href="([^"]*cfvar\d+z[^"]*)"', link_value, fragment)
    fragment = _TOKEN_RE.sub(lambda m: html.escape(tokens[int(m.group(1))]), fragment)

    return RenderedEmail(
        heading=html.escape(substitute_text(heading_source, values)),
        html=fragment,
        text=_plain_text(body, values),
    )
