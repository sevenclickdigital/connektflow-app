import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from apps.accounts import impersonation

register = template.Library()


@register.simple_tag(takes_context=True)
def support_chat(context):
    """``{% support_chat as chat %}``: whether the chat widget is drawn on this page, and the open chat's status.

    None for anyone who is not a customer using their own account: signed
    out, a team account, or an operator inside a customer's account (who
    must not write in the customer's name). Otherwise ``{"status": ...}``,
    empty when the person has no open chat. One query per page.
    """
    from apps.support.services import open_chat_of

    request = context.get("request")
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated) or user.is_staff or impersonation.is_impersonating(request):
        return None
    chat = open_chat_of(user)
    return {"status": chat.status if chat else ""}


# The chat's formatting, as in WhatsApp: *bold*, _italic_, ~strike~, `code`;
# web addresses become links. Mirrored in JavaScript by
# support/partials/_message_format.html (window.cfMessageFormat.render):
# change both together.
_EDGE_BEFORE = r"(^|[\s(])"
_EDGE_AFTER = r"(?=$|[\s.,!?;:)])"
_STYLES = (
    (re.compile(r"`([^`\n]+)`"), r"<code>\1</code>"),
    (re.compile(_EDGE_BEFORE + r"\*(\S(?:[^*\n]*\S)?)\*" + _EDGE_AFTER), r"\1<strong>\2</strong>"),
    (re.compile(_EDGE_BEFORE + r"_(\S(?:[^_\n]*\S)?)_" + _EDGE_AFTER), r"\1<em>\2</em>"),
    (re.compile(_EDGE_BEFORE + r"~(\S(?:[^~\n]*\S)?)~" + _EDGE_AFTER), r"\1<s>\2</s>"),
)
_LINK = re.compile(r"(^|[\s(])(https?://[^\s<]+[^\s<.,!?;:)])")


@register.filter
def message_format(value):
    """``{{ message.body|message_format }}``: the message escaped, then its formatting marks as tags."""
    html = str(escape(value or ""))
    for pattern, replacement in _STYLES:
        html = pattern.sub(replacement, html)
    html = _LINK.sub(r'\1<a href="\2" target="_blank" rel="noopener noreferrer" class="underline">\2</a>', html)
    return mark_safe(html)  # noqa: S308 - escaped above; only the fixed tags are added
