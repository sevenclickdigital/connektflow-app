"""The default version of a system e-mail, written as Markdown for the editor to start from.

The code template is rendered with the e-mail's sample context, the body
between the layout's ``cf-content`` markers is turned into Markdown (the same
conventions ``markdown_mail`` reads back: the heading line, ``**bold**``,
lists, ``>`` panels, the ``[button: Label](url)`` button and the
``<small>`` line under it), and each sample
value is put back as the variable it came from. Reading the template itself,
rather than keeping a second copy of every text, means the starting point is
always what is being sent today. Parts that depend on the case (shown only
when something applies) come out as they render for the sample.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.translation import gettext

from apps.common.emails import email_context, render_email, supported_language

from . import catalog

logger = logging.getLogger(__name__)

_CONTENT_RE = re.compile(r"<!--cf-content-->(.*?)<!--/cf-content-->", re.S)
_HEADING_RE = re.compile(r'<h1 class="cf-h1 cf-h1-desktop"[^>]*>(.*?)</h1>', re.S)
_VOID = {"br", "img", "hr", "meta", "input"}


def _subjects():
    """Subjects of the e-mails built with ``build_email``: the call sites' own sentences."""
    return {
        "members/email/invite": gettext("Invitation to the organization %(name)s on Connekt Flow")
        % {"name": "{{ organization_name }}"},
        "organizations/email/deletion_scheduled": gettext("The organization %(name)s will be deleted on %(date)s")
        % {"name": "{{ organization_name }}", "date": "{{ deletion_date }}"},
        "onboarding/email/connection_link": gettext("%(org)s - Connect your social accounts")
        % {"org": "{{ organization_name }}"},
        "client_portal/email/magic_link": gettext("%(workspace)s - Posts ready for your review")
        % {"workspace": "{{ brand }}"},
        "notifications/email/notification": "{{ title }}",
        "billing/email/payment_confirmed": gettext("Payment confirmed: your plan is active"),
        "billing/email/archive_removal_warning": gettext("Your data on %(name)s will be removed soon")
        % {"name": "{{ organization_name }}"},
        "affiliates/email/affiliate_added": gettext("You are now an affiliate"),
        "affiliates/email/terms_changed": gettext("Your affiliate terms have changed"),
        "affiliates/email/payout_requested": gettext("We received your withdrawal request of R$ %(amount)s")
        % {"amount": "{{ amount }}"},
        "affiliates/email/payout_paid": gettext("Your withdrawal of R$ %(amount)s is in your account")
        % {"amount": "{{ amount }}"},
        "affiliates/email/payout_requested_team": gettext("%(name)s asked for a withdrawal of R$ %(amount)s")
        % {"name": "{{ affiliate_name }}", "amount": "{{ amount }}"},
    }


class _Node:
    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.parent = parent
        self.children: list = []


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in _VOID:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.current = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def _text(node) -> str:
    if isinstance(node, str):
        return node
    return "".join(_text(child) for child in node.children)


def _find(node, tag):
    for child in node.children:
        if isinstance(child, _Node):
            if child.tag == tag:
                return child
            found = _find(child, tag)
            if found is not None:
                return found
    return None


def _card_markdown(table) -> str:
    """The withdrawal card back as a Markdown table: its top cell is the header row."""
    rows = []
    for tr in _all(table, "tr"):
        cells = [child for child in tr.children if isinstance(child, _Node) and child.tag == "td"]
        if len(cells) == 1:
            spans = [child for child in cells[0].children if isinstance(child, _Node) and child.tag == "span"]
            parts = [_inline(span).strip() for span in spans] if spans else [_inline(cells[0]).strip(), ""]
        else:
            parts = [_inline(cell).strip() for cell in cells[:2]]
        rows.append("| " + " | ".join(part.replace("|", "\\|") for part in (parts + [""])[:2]) + " |")
    if not rows:
        return ""
    return "\n\n" + "\n".join([rows[0], "|---|---|", *rows[1:]]) + "\n\n"


def _all(node, tag):
    for child in node.children:
        if isinstance(child, _Node):
            if child.tag == tag:
                yield child
            yield from _all(child, tag)


def _is_panel(node) -> bool:
    """A cell with a background of its own: the layout's boxes (steps, codes, plan details)."""
    style = (node.attrs.get("style") or "").replace(" ", "").lower()
    return "background" in style or bool(node.attrs.get("bgcolor"))


def _inline(node) -> str:
    """The children of ``node`` as one block of text, line breaks kept."""
    text = "".join(_render(child) for child in node.children)
    return re.sub(r"[ \t\r\f\v]*\n[ \t\r\f\v]*", "\n", re.sub(r"[ \t\r\f\v]+", " ", text))


def _render(node) -> str:
    if isinstance(node, str):
        return re.sub(r"\s+", " ", node)
    tag = node.tag
    if "hidden" in node.attrs or tag in {"style", "script", "head"}:
        return ""
    if "cf-btn-fallback" in (node.attrs.get("class") or ""):
        # The "copy this link" line: written out, so it can be changed or removed.
        return f"\n\n<small>{_inline(node).strip()}</small>\n\n"
    if tag == "table" and "cf-btn" in (node.attrs.get("class") or ""):
        link = _find(node, "a")
        label = re.sub(r"\s+", " ", _text(link)).strip() if link else ""
        href = link.attrs.get("href", "") if link else ""
        return f"\n\n[{gettext('button:')} {label}]({href})\n\n"
    style = (node.attrs.get("style") or "").replace(" ", "").lower()
    if tag == "table" and "border:1pxsolid#e9e5fb" in style:
        return _card_markdown(node)
    if tag == "p" and "letter-spacing" in style and "font-size:28px" in style:
        return f"\n\n`{_inline(node).strip()}`\n\n"
    if tag == "p" and "font-size:13px" in style and "#64748b" in style:
        return f"\n\n<small>{_inline(node).strip()}</small>\n\n"
    inner = "".join(_render(child) for child in node.children)
    if tag == "br":
        return "\n"
    if tag in {"strong", "b"}:
        return f"**{inner.strip()}**" if inner.strip() else ""
    if tag in {"em", "i"}:
        return f"*{inner.strip()}*" if inner.strip() else ""
    if tag == "a":
        href = node.attrs.get("href", "")
        return f"[{inner.strip()}]({href})" if href else inner
    if tag in {"h1", "h2", "h3"}:
        return f"\n\n## {inner.strip()}\n\n"
    if tag in {"ul", "ol"}:
        items = [_inline(child).strip() for child in node.children if isinstance(child, _Node) and child.tag == "li"]
        return "\n\n" + "\n".join(f"- {item}" for item in items) + "\n\n"
    if tag in {"p", "div", "td", "table"} and _is_panel(node):
        text = _inline(node).strip()
        if "font-size:13px" in style:
            text = f"<small>{text}</small>"
        lines = [line.strip() for line in text.split("\n")]
        return "\n\n" + "\n".join(f"> {line}" if line else ">" for line in lines) + "\n\n"
    if tag in {"p", "div", "td", "tr", "table"}:
        return f"\n\n{inner.strip()}\n\n" if inner.strip() else ""
    return inner


def html_to_markdown(fragment: str) -> str:
    builder = _TreeBuilder()
    builder.feed(fragment)
    text = _render(builder.root)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _as_variables(text: str, values: dict[str, str]) -> str:
    """Put each sample value back as ``{{ name }}``, longest first so one value never splits another."""
    for name, value in sorted(values.items(), key=lambda item: -len(item[1])):
        # The product name stays written out; a short value is only safe to
        # look for when it is a number (a count such as "3 brands").
        if name == "app_name" or not value or (len(value) < 3 and not value.isdigit()):
            continue
        text = re.sub(rf"(?<![\w@.]){re.escape(value)}(?![\w@])", "{{ " + name + " }}", text)
    return text


def code_template(key: str) -> str:
    """The template prefix of a system e-mail's code version (allauth's files end in ``_message``)."""
    return f"{key}_message" if key.startswith(("account/", "socialaccount/")) else key


def default_version(entry: catalog.SystemEmail) -> tuple[str, str]:
    """``(subject, markdown)`` of ``entry``'s default version, in the active language."""
    sample = entry.sample()
    values = catalog.resolve(entry, email_context(**sample))
    _text_body, html = render_email(code_template(entry.key), sample)

    heading_match = _HEADING_RE.search(html)
    heading = html_to_markdown(heading_match.group(1)) if heading_match else ""
    content_match = _CONTENT_RE.search(html)
    body = html_to_markdown(content_match.group(1)) if content_match else ""
    markdown = f"# {heading}\n\n{body}" if heading else body

    if entry.key.startswith(("account/", "socialaccount/")):
        subject = render_to_string(f"{entry.key}_subject.txt", email_context(**sample)).strip()
    else:
        subject = _subjects().get(entry.key, "")
    return _as_variables(subject, values), _as_variables(markdown, values)


def language_codes() -> list[str]:
    """The languages every system e-mail has a version in: ``settings.LANGUAGES``, in that order."""
    return [code for code, _name in settings.LANGUAGES]


def language_of_send() -> str:
    """The ``settings.LANGUAGES`` code of the language active now (the one an e-mail is being built in)."""
    return supported_language(translation.get_language())


def default_in(entry: catalog.SystemEmail, language: str) -> tuple[str, str]:
    """``(subject, markdown)`` of ``entry``'s code template in ``language``."""
    with translation.override(language):
        return default_version(entry)


def ensure_versions(keys: list[str] | None = None) -> None:
    """Store the code template's text as the version of each e-mail (of ``keys``, else all) in each language.

    Only the missing (e-mail, language) rows are created, switched off, so
    nothing changes in what is sent and a text the team edited is never
    replaced. A language added to ``settings.LANGUAGES`` gets its versions the
    next time this runs.
    """
    from .models import SystemEmailOverride

    entries = [entry for entry in catalog.SYSTEM_EMAILS if keys is None or entry.key in keys]
    existing = set(
        SystemEmailOverride.objects.filter(key__in=[entry.key for entry in entries]).values_list("key", "language")
    )
    missing = []
    for entry in entries:
        for language in language_codes():
            if (entry.key, language) in existing:
                continue
            try:
                subject, body = default_in(entry, language)
            except Exception:  # noqa: BLE001 — one template that fails to convert must not hide the others
                logger.exception("Default text of %s (%s) could not be converted", entry.key, language)
                continue
            missing.append(
                SystemEmailOverride(
                    key=entry.key,
                    language=language,
                    subject=subject[:200],
                    body_markdown=body,
                    is_default_text=True,
                )
            )
    SystemEmailOverride.objects.bulk_create(missing, ignore_conflicts=True)
