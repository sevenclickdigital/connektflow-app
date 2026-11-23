"""Message templates of a connected WhatsApp number.

A template belongs to the WhatsApp Business Account behind the number, lives
at Meta and is reviewed there: nothing is stored here. The screen lists what
the account has and creates text-only templates.
"""

from django import forms
from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _

from providers.exceptions import APIError, ProviderError
from providers.types import MessageTemplate

from .error_messages import friendly_template_error
from .webhooks import _account_provider

#: The categories a text-only template can be filed under. Authentication
#: templates have a fixed body and a code button, which this does not build.
CATEGORIES = [
    ("UTILITY", _("Utility: about an order, a booking or an account")),
    ("MARKETING", _("Marketing: offers, news and invitations")),
]

LANGUAGES = [
    ("pt_BR", _("Portuguese (Brazil)")),
    ("en_US", _("English (US)")),
    ("es", _("Spanish")),
]

#: How Meta's review states read, and the pill each one gets.
STATUSES = {
    "APPROVED": (_("Approved"), "pill-success"),
    "PENDING": (_("In review"), "pill-warning"),
    "IN_APPEAL": (_("In review"), "pill-warning"),
    "REJECTED": (_("Rejected"), "pill-error"),
    "PAUSED": (_("Paused"), "pill-neutral"),
    "DISABLED": (_("Disabled"), "pill-neutral"),
}


class TemplateError(Exception):
    """Meta did not list or create the templates; the message is safe to show."""


class MessageTemplateForm(forms.Form):
    name = forms.CharField(
        label=_("Name"),
        max_length=512,
        validators=[RegexValidator(r"^[a-z0-9_]+$", _("Use only lowercase letters, numbers and underscores."))],
        help_text=_("Lowercase letters, numbers and underscores, for example order_shipped."),
        widget=forms.TextInput(attrs={"class": "form-input-styled w-full", "autocomplete": "off"}),
    )
    category = forms.ChoiceField(
        label=_("Category"),
        choices=CATEGORIES,
        widget=forms.Select(attrs={"class": "form-input-styled bb-select cursor-pointer w-full"}),
    )
    language = forms.ChoiceField(
        label=_("Language"),
        choices=LANGUAGES,
        widget=forms.Select(attrs={"class": "form-input-styled bb-select cursor-pointer w-full"}),
    )
    body = forms.CharField(
        label=_("Message"),
        max_length=1024,
        widget=forms.Textarea(attrs={"class": "form-input-styled w-full", "rows": 5}),
    )

    def clean_body(self):
        body = self.cleaned_data["body"].strip()
        if "{{" in body:
            raise forms.ValidationError(_("Variables such as {{1}} can't be used in a template created here."))
        return body


def _error_text(exc: ProviderError) -> str:
    """Meta's own explanation of a refused template when it wrote one for the person, else ours.

    A refusal over the template itself (a name already in use in that
    language, a body the category does not allow) comes with
    ``error_user_msg``, which says what to change better than any rewrite.
    """
    error = (getattr(exc, "raw_response", None) or {}).get("error")
    if isinstance(exc, APIError) and exc.status_code == 400 and isinstance(error, dict):
        explained = str(error.get("error_user_msg") or "").strip()
        if explained:
            return explained
    return friendly_template_error(exc)


def list_templates(account) -> list[dict]:
    """The account's templates as the screen shows them. Raises ``TemplateError``."""
    try:
        templates = _account_provider(account).list_message_templates(account.oauth_access_token)
    except ProviderError as exc:
        raise TemplateError(_error_text(exc)) from exc
    languages = dict(LANGUAGES)
    rows = []
    for template in templates:
        label, pill = STATUSES.get(template.status, (template.status.replace("_", " ").capitalize(), "pill-neutral"))
        rows.append(
            {
                "name": template.name,
                "category": template.category.capitalize(),
                "language": languages.get(template.language, template.language),
                "status": label,
                "pill": pill,
                "body": template.body,
            }
        )
    return rows


def create_template(account, *, name: str, category: str, language: str, body: str) -> MessageTemplate:
    """Create a template in the account's WhatsApp Business Account. Raises ``TemplateError``."""
    try:
        return _account_provider(account).create_message_template(
            account.oauth_access_token, name=name, language=language, category=category, body=body
        )
    except ProviderError as exc:
        raise TemplateError(_error_text(exc)) from exc
