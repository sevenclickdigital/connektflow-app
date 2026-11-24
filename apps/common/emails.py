"""Building outbound email: the shared layout, the brand context and the message.

Every email the product sends is a pair of templates, ``<prefix>.html``
extending ``emails/base.html`` (logo, centred card, legal footer) and
``<prefix>.txt`` extending ``emails/base.txt`` (the same footer as text).
``build_email`` renders both with the brand context and returns the message;
sending stays with the caller, through ``apps.common.mail.send_or_raise``, so the
outbound budget keeps deciding what leaves.

allauth renders its own mail (``apps.accounts.adapters.AccountAdapter``) and
merges ``email_context()`` there, so its templates see the same variables.

Each person receives e-mail in the language of their profile
(``User.language``). The subject is usually written by the caller, so the
caller builds the message inside ``in_language_of(recipient)``;
``build_email`` and allauth's ``render_mail`` switch to it as well.

Variables every email template can use:

- ``brand_name``, ``app_url``, ``logo_url``, ``mascot_url`` (Moti waving, beside the logo)
- ``privacy_url``, ``terms_url``, ``support_email``
- ``company_name``, ``company_document`` (CNPJ), ``company_address_lines``,
  ``company_phone`` with ``company_phone_digits``
  (for the ``tel:`` and WhatsApp links)
- ``current_year``
- ``unsubscribe_url`` / ``preferences_url``: only on notification mail, which
  turns on the unsubscribe lines in the footer.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.utils import timezone, translation

logger = logging.getLogger(__name__)

# The wordmark all in the brand purple (#7C3AED) on a transparent canvas: one
# colour that reads on our white card and also where a mail client darkens the
# card but leaves images as they are (Gmail, Outlook), which is where the
# two-tone wordmark's dark letters disappear. Made from the alpha of
# branding/logo/connektflow-wordmark-dark.png.
LOGO_PATH = "img/email/connektflow-wordmark-dark.png"
# PNG, not the site's WebP: Outlook and older mail clients don't show WebP.
MASCOT_PATH = "img/email/moti-acenando.png"


def _app_url() -> str:
    return getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")


def _absolute_static(path: str) -> str:
    """Absolute URL of a static file, for an ``<img>`` a mail client fetches.

    Mail clients have no base URL, so the path must carry the host. With the
    manifest storage a file missing from the manifest raises ``ValueError``;
    the unhashed path still resolves through WhiteNoise, so fall back to it
    rather than failing the send over a logo.
    """
    try:
        url = static(path)
    except ValueError:
        logger.warning("Static file %s is not in the manifest; using the unhashed URL", path)
        url = f"{settings.STATIC_URL.rstrip('/')}/{path}"
    return url if url.startswith(("http://", "https://")) else f"{_app_url()}{url}"


def _address_lines(address: str) -> list[str]:
    """``LEGAL_COMPANY_ADDRESS`` split on ``|``, one footer line per part."""
    return [part.strip() for part in address.split("|") if part.strip()]


def email_context(**extra) -> dict:
    """The brand and legal variables the email layout needs, plus ``extra``."""
    from apps.common.legal_views import APP_NAME, PRIVACY_POLICY_URL, TERMS_OF_SERVICE_URL, contact_email

    app_url = _app_url()
    phone = getattr(settings, "LEGAL_COMPANY_PHONE", "")
    context = {
        "brand_name": APP_NAME,
        "app_url": app_url,
        "logo_url": _absolute_static(LOGO_PATH),
        "mascot_url": _absolute_static(MASCOT_PATH),
        "privacy_url": PRIVACY_POLICY_URL,
        "terms_url": TERMS_OF_SERVICE_URL,
        "support_email": contact_email(),
        "company_name": getattr(settings, "LEGAL_COMPANY_NAME", "") or APP_NAME,
        "company_document": getattr(settings, "LEGAL_COMPANY_DOCUMENT", ""),
        "company_address_lines": _address_lines(getattr(settings, "LEGAL_COMPANY_ADDRESS", "")),
        "company_phone": phone,
        "company_phone_digits": re.sub(r"\D", "", phone),
        "current_year": timezone.now().year,
    }
    context.update(extra)
    return context


def supported_language(code: str | None) -> str:
    """``code`` as one of ``settings.LANGUAGES`` (``en`` becomes ``en-us``), else the product's default."""
    try:
        return translation.get_supported_language_variant(code or settings.LANGUAGE_CODE)
    except LookupError:
        return settings.LANGUAGE_CODE


def language_of(recipient) -> str:
    """The language to write to ``recipient`` (a user or an e-mail address) in.

    A person with an account gets the language of their profile; an address
    with no account (someone invited, a client of a brand) gets the language
    active now, the one of whoever made the e-mail go.
    """
    from django.contrib.auth import get_user_model

    language = getattr(recipient, "language", None)
    if language is None and isinstance(recipient, str) and recipient:
        language = get_user_model().objects.filter(email__iexact=recipient).values_list("language", flat=True).first()
    return supported_language(language or translation.get_language())


@contextmanager
def in_language_of(recipient):
    """Run the block (subject, body) in ``recipient``'s language (``language_of``)."""
    with translation.override(language_of(recipient)):
        yield


def render_email(template_prefix: str, context: dict) -> tuple[str, str]:
    """Render ``<prefix>.txt`` and ``<prefix>.html`` with the brand context."""
    full = email_context(**context)
    return (
        render_to_string(f"{template_prefix}.txt", full).strip(),
        render_to_string(f"{template_prefix}.html", full),
    )


def build_email(
    *,
    subject: str,
    template_prefix: str,
    context: dict,
    to: list[str],
    headers: dict[str, str] | None = None,
) -> EmailMultiAlternatives:
    """A text + HTML message from ``template_prefix``, ready to send.

    ``headers`` carries the mail class (``apps.common.mail.transactional()``)
    or the ``List-Unsubscribe`` pair of notification mail. The body is
    rendered in the recipient's language (``language_of``); a message to
    several people stays in the language active now. When the team has an
    active Markdown version of this e-mail in that language
    (``apps.email_center``), its subject and body replace the template's.
    """
    from apps.email_center.rendering import render_system_override

    with in_language_of(to[0] if len(to) == 1 else ""):
        override = render_system_override(template_prefix, context)
        if override is not None:
            subject, text_content, html_content = override
        else:
            text_content, html_content = render_email(template_prefix, context)
    msg = EmailMultiAlternatives(
        subject=" ".join(str(subject).split()),
        body=text_content,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
        to=to,
        headers=headers or {},
    )
    msg.attach_alternative(html_content, "text/html")
    return msg
