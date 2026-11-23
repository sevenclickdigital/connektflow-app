"""Forms for the account centre (profile)."""

from django import forms
from django.utils.translation import gettext_lazy as _

from .address import cep_digits
from .models import User

# Brazilian states (UF), the only addresses the product bills today.
STATE_CHOICES = [
    ("", _("Select")),
    ("AC", "Acre"),
    ("AL", "Alagoas"),
    ("AP", "Amapá"),
    ("AM", "Amazonas"),
    ("BA", "Bahia"),
    ("CE", "Ceará"),
    ("DF", "Distrito Federal"),
    ("ES", "Espírito Santo"),
    ("GO", "Goiás"),
    ("MA", "Maranhão"),
    ("MT", "Mato Grosso"),
    ("MS", "Mato Grosso do Sul"),
    ("MG", "Minas Gerais"),
    ("PA", "Pará"),
    ("PB", "Paraíba"),
    ("PR", "Paraná"),
    ("PE", "Pernambuco"),
    ("PI", "Piauí"),
    ("RJ", "Rio de Janeiro"),
    ("RN", "Rio Grande do Norte"),
    ("RS", "Rio Grande do Sul"),
    ("RO", "Rondônia"),
    ("RR", "Roraima"),
    ("SC", "Santa Catarina"),
    ("SP", "São Paulo"),
    ("SE", "Sergipe"),
    ("TO", "Tocantins"),
]

# Filled together or not at all: a half address is no use on an invoice.
# Complement is the one optional part.
ADDRESS_REQUIRED = (
    "address_postal_code",
    "address_street",
    "address_number",
    "address_neighborhood",
    "address_city",
    "address_state",
)


class ProfileForm(forms.ModelForm):
    """Name and postal address of the person behind the account.

    Saving also writes ``User.name`` (first + last name), which is what the
    rest of the product shows.
    """

    address_state = forms.ChoiceField(choices=STATE_CHOICES, required=False, label=_("State"))

    class Meta:
        model = User
        fields = [
            "first_name",
            "last_name",
            "address_postal_code",
            "address_street",
            "address_number",
            "address_complement",
            "address_neighborhood",
            "address_city",
            "address_state",
        ]
        labels = {
            "first_name": _("First name"),
            "last_name": _("Last name"),
            "address_postal_code": _("Postal code (CEP)"),
            "address_street": _("Street"),
            "address_number": _("Number"),
            "address_complement": _("Complement"),
            "address_neighborhood": _("Neighborhood"),
            "address_city": _("City"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        autocomplete = {
            "first_name": "given-name",
            "last_name": "family-name",
            "address_postal_code": "postal-code",
            "address_street": "address-line1",
            "address_complement": "address-line2",
            "address_neighborhood": "address-level3",
            "address_city": "address-level2",
            "address_state": "address-level1",
        }
        for name, field in self.fields.items():
            css = "form-input-styled bb-select cursor-pointer" if name == "address_state" else "form-input-styled"
            field.widget.attrs.update({"class": css, "autocomplete": autocomplete.get(name, "off")})
        # data-mask: the shared masks in base.html keep it as 00000-000.
        self.fields["address_postal_code"].widget.attrs.update(
            {"inputmode": "numeric", "placeholder": "00000-000", "data-mask": "cep"}
        )
        # Accounts created before first/last name existed only have ``name``:
        # offer it split, so the person confirms rather than retypes it.
        user = self.instance
        if not self.is_bound and not user.first_name and not user.last_name and user.name:
            first, _sep, last = user.name.strip().partition(" ")
            self.initial["first_name"] = first
            self.initial["last_name"] = last.strip()

    def clean_address_postal_code(self):
        raw = self.cleaned_data.get("address_postal_code", "").strip()
        if not raw:
            return ""
        digits = cep_digits(raw)
        if not digits:
            raise forms.ValidationError(_("Enter a CEP with 8 digits, like 01310-100."))
        return f"{digits[:5]}-{digits[5:]}"

    def clean(self):
        cleaned = super().clean()
        for field in self.fields:
            if isinstance(cleaned.get(field), str):
                cleaned[field] = cleaned[field].strip()
        started = any(cleaned.get(f) for f in (*ADDRESS_REQUIRED, "address_complement"))
        if started:
            for field in ADDRESS_REQUIRED:
                if not cleaned.get(field) and field not in self.errors:
                    self.add_error(field, _("Required to complete the address."))
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.name = f"{user.first_name} {user.last_name}".strip()
        if commit:
            user.save(update_fields=[*self.Meta.fields, "name", "updated_at"])
        return user


class SignupNameForm(forms.Form):
    """The person's name, asked at sign-up so the first e-mails already greet them by it.

    allauth's sign-up form inherits it (``ACCOUNT_SIGNUP_FORM_CLASS``) and
    calls ``signup`` once the user is saved, before the welcome e-mail goes
    out. The name is split as the profile form keeps it: the first word is
    the first name, the rest the last name, and ``User.name`` the whole.
    """

    # allauth orders its sign-up fields by this: the name before the e-mail and password.
    field_order = ["full_name", "email", "password1"]

    full_name = forms.CharField(
        label=_("Your name"),
        max_length=255,
        widget=forms.TextInput(attrs={"autocomplete": "name", "placeholder": _("e.g. Ana Souza")}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The Google sign-up form (allauth's social sign-up) comes with the name the profile gave.
        sociallogin = getattr(self, "sociallogin", None)
        if sociallogin is not None and not self.is_bound:
            user = sociallogin.user
            self.initial.setdefault("full_name", user.name or f"{user.first_name} {user.last_name}".strip())

    def clean_full_name(self):
        name = " ".join(self.cleaned_data["full_name"].split())
        if not name:
            raise forms.ValidationError(_("Enter your name."))
        return name

    def signup(self, request, user):
        name = self.cleaned_data["full_name"]
        first, _sep, last = name.partition(" ")
        user.first_name, user.last_name, user.name = first[:150], last[:150], name
        user.save(update_fields=["first_name", "last_name", "name", "updated_at"])
