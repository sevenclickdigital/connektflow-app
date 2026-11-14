import re

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import ChannelKind, Contact
from .services import ChannelInput, clean_tags, normalize_channel_value

MAX_NOTES_LENGTH = 500
FIELD_SIZE = "text-sm px-3 py-2 border rounded-lg"
MAX_CHANNELS = 10
MAX_PHOTO_BYTES = 2 * 1024 * 1024
PHOTO_TYPES = ("image/jpeg", "image/png", "image/webp")

BRAZIL_STATES = [
    "AC",
    "AL",
    "AP",
    "AM",
    "BA",
    "CE",
    "DF",
    "ES",
    "GO",
    "MA",
    "MT",
    "MS",
    "MG",
    "PA",
    "PB",
    "PR",
    "PE",
    "PI",
    "RJ",
    "RN",
    "RS",
    "RO",
    "RR",
    "SC",
    "SP",
    "SE",
    "TO",
]


def _check_digit(digits: str, weights: list[int]) -> int:
    rest = sum(int(d) * w for d, w in zip(digits, weights, strict=False)) % 11
    return 0 if rest < 2 else 11 - rest


def is_valid_cpf(digits: str) -> bool:
    if len(digits) != 11 or digits == digits[0] * 11:
        return False
    first = _check_digit(digits[:9], list(range(10, 1, -1)))
    second = _check_digit(digits[:10], list(range(11, 1, -1)))
    return digits[-2:] == f"{first}{second}"


def is_valid_cnpj(digits: str) -> bool:
    if len(digits) != 14 or digits == digits[0] * 14:
        return False
    weights = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    first = _check_digit(digits[:12], weights)
    second = _check_digit(digits[:13], [6, *weights])
    return digits[-2:] == f"{first}{second}"


class ContactForm(forms.ModelForm):
    """The contact's own fields. Channels and the photo are read by the view
    (``parse_channels`` / ``clean_photo``) because they are not plain columns."""

    tags = forms.CharField(required=False)

    class Meta:
        model = Contact
        fields = [
            "name",
            "kind",
            "tax_id",
            "birth_date",
            "gender",
            "company",
            "status",
            "funnel",
            "segment",
            "source",
            "engagement_manual",
            "address",
            "city",
            "state",
            "postal_code",
            "notes",
        ]
        widgets = {
            "birth_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "name": _("Full name"),
            "kind": _("Contact type"),
            "birth_date": _("Date of birth"),
            "gender": _("Gender"),
            "company": _("Company"),
            "status": _("Status"),
            "funnel": _("Funnel"),
            "segment": _("Segment"),
            "source": _("Contact source"),
            "engagement_manual": _("Engagement level"),
            "address": _("Address"),
            "city": _("City"),
            "state": _("State"),
            "postal_code": _("CEP"),
            "notes": _("Notes"),
        }

    # Wider than the stored digits so a formatted CNPJ (18 characters) is accepted.
    tax_id = forms.CharField(required=False, max_length=18, label=_("CPF / CNPJ"))
    postal_code = forms.CharField(required=False, max_length=9, label=_("CEP"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial.setdefault("tags", ", ".join(self.instance.tags or []))
        self.fields["state"].widget = forms.Select(choices=[("", "—"), *[(uf, uf) for uf in BRAZIL_STATES]])
        self.fields["engagement_manual"].choices = [("", _("Automatic")), *Contact.Engagement.choices]
        self.fields["gender"].choices = [("", "—"), *Contact.Gender.choices]
        placeholders = {
            "tax_id": "000.000.000-00",
            "postal_code": "00000-000",
            "funnel": _("e.g. Sales"),
            "segment": _("e.g. Architects"),
        }
        for name, field in self.fields.items():
            # The composer's field size (text-sm, px-3 py-2, 1px border) on the shared input style.
            css = f"form-input-styled {FIELD_SIZE}"
            if isinstance(field.widget, forms.Select):
                css += " bb-select pr-9"
            field.widget.attrs.setdefault("class", css)
            if name in placeholders:
                field.widget.attrs["placeholder"] = placeholders[name]
        self.fields["funnel"].widget.attrs["list"] = "contact-funnel-suggestions"
        self.fields["segment"].widget.attrs["list"] = "contact-segment-suggestions"

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise ValidationError(_("Enter the contact's name."))
        return name

    def clean_tax_id(self):
        digits = re.sub(r"\D", "", self.cleaned_data.get("tax_id") or "")
        if not digits:
            return ""
        if len(digits) == 11 and is_valid_cpf(digits):
            return digits
        if len(digits) == 14 and is_valid_cnpj(digits):
            return digits
        raise ValidationError(_("Enter a valid CPF or CNPJ."))

    def clean_state(self):
        state = (self.cleaned_data.get("state") or "").strip().upper()
        if state and state not in BRAZIL_STATES:
            raise ValidationError(_("Choose a Brazilian state."))
        return state

    def clean_postal_code(self):
        digits = re.sub(r"\D", "", self.cleaned_data.get("postal_code") or "")
        if not digits:
            return ""
        if len(digits) != 8:
            raise ValidationError(_("A CEP has 8 digits."))
        return f"{digits[:5]}-{digits[5:]}"

    def clean_notes(self):
        notes = (self.cleaned_data.get("notes") or "").strip()
        if len(notes) > MAX_NOTES_LENGTH:
            raise ValidationError(_("Notes can have up to %(max)s characters.") % {"max": MAX_NOTES_LENGTH})
        return notes

    def clean_funnel(self):
        return (self.cleaned_data.get("funnel") or "").strip()

    def clean_segment(self):
        return (self.cleaned_data.get("segment") or "").strip()

    def clean_tags(self):
        return clean_tags(self.cleaned_data.get("tags") or "")

    def save(self, commit=True):
        self.instance.tags = self.cleaned_data["tags"]
        self.instance.refresh_engagement()
        return super().save(commit=commit)


def parse_channels(post) -> tuple[list[ChannelInput], list[str]]:
    """Channels from the form's parallel ``channel_kind`` / ``channel_value``
    lists; ``channel_primary`` is the index of the main one. Returns the
    channels and the errors to show."""
    kinds = post.getlist("channel_kind")
    values = post.getlist("channel_value")
    primary = post.get("channel_primary", "0")
    channels: list[ChannelInput] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, (kind, raw) in enumerate(zip(kinds, values, strict=False)):
        if kind not in ChannelKind.values:
            continue
        value = normalize_channel_value(kind, raw)[:255]
        if not value:
            continue
        if kind == ChannelKind.EMAIL and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            errors.append(str(_("%(value)s is not a valid email address.") % {"value": value}))
            continue
        if kind in (ChannelKind.WHATSAPP, ChannelKind.PHONE) and not 10 <= len(value) - 1 <= 15:
            errors.append(str(_("%(value)s is not a valid phone number. Include the area code.") % {"value": raw}))
            continue
        if (kind, value) in seen:
            continue
        seen.add((kind, value))
        channels.append(ChannelInput(kind, value, is_primary=str(index) == primary))
    if len(channels) > MAX_CHANNELS:
        errors.append(str(_("A contact can have up to %(max)s channels.") % {"max": MAX_CHANNELS}))
    if channels and not any(c.is_primary for c in channels):
        channels[0].is_primary = True
    return channels, errors


def clean_photo(upload) -> str:
    """The error for an uploaded photo, or "" when it can be stored."""
    if upload.content_type not in PHOTO_TYPES:
        return str(_("The photo must be a JPEG, PNG or WebP image."))
    if upload.size > MAX_PHOTO_BYTES:
        return str(_("The photo must be under 2 MB."))
    from PIL import Image

    try:
        with Image.open(upload) as img:
            img.verify()
    except Exception:
        return str(_("Could not read the image file."))
    upload.seek(0)
    return ""
