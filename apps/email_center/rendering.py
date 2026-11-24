"""Building the subject, text and HTML of an e-mail written in Markdown.

``render_system_override`` is called by the two places every system e-mail
goes through (``apps.common.emails.build_email`` and
``apps.accounts.adapters.AccountAdapter.render_mail``): when the team has an
active Markdown version of that e-mail, it is used instead of the code
template. A version that fails to render is logged and the code template is
used, so an edit can never stop a sign-in code or a security notice from
going out.
"""

from __future__ import annotations

import html
import logging
import re

from django.utils.html import strip_tags
from django.utils.safestring import mark_safe

from apps.common.emails import email_context, render_email

from . import catalog, markdown_mail
from .models import SystemEmailOverride

logger = logging.getLogger(__name__)

TEMPLATE_PREFIX = "email_center/email/markdown"
_REASON_RE = re.compile(r"<!--cf-reason-->(.*?)<!--/cf-reason-->", re.S)


def default_reason(key: str, context: dict) -> str:
    """The footer's "why you got this" of system e-mail ``key``'s default template, as rendered for ``context``.

    An edited version only replaces the text of the e-mail; the reason stays
    the one the default gives for this send.
    """
    from .defaults import code_template

    _text, rendered = render_email(code_template(key), context)
    match = _REASON_RE.search(rendered)
    return match.group(1).strip() if match else ""


def render_message(
    *, subject: str, body_markdown: str, values: dict, context: dict, reason_text: str = "", reason_html: str = ""
):
    """``(subject, text, html)`` of a Markdown e-mail in the product's layout.

    ``reason_html`` is the footer's "why you got this" taken from a default
    template (already rendered and escaped); ``reason_text`` is plain text.
    """
    rendered = markdown_mail.render(body_markdown, values)
    final_subject = " ".join(markdown_mail.substitute_text(subject, values).split())
    text, body = render_email(
        TEMPLATE_PREFIX,
        {
            **context,
            "subject": final_subject,
            "markdown_heading": rendered.heading,
            "markdown_html": rendered.html,
            "markdown_text": rendered.text,
            "reason_text": reason_text or html.unescape(strip_tags(reason_html)).strip(),
            "reason_html": mark_safe(reason_html),
        },
    )
    return final_subject, text, body


def render_system_override(key: str, context: dict):
    """``(subject, text, html)`` of the active Markdown version of system e-mail ``key``, else None."""
    entry = catalog.get(key)
    if entry is None:
        return None
    override = SystemEmailOverride.objects.filter(key=key, is_active=True).first()
    if override is None:
        return None
    try:
        values = catalog.resolve(entry, email_context(**context))
        return render_message(
            subject=override.subject,
            body_markdown=override.body_markdown,
            values=values,
            context=context,
            reason_html=default_reason(key, context),
        )
    except Exception:
        logger.exception("Markdown version of %s failed to render; sending the code template", key)
        return None
