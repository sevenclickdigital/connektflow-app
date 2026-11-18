import re
from decimal import Decimal

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.media_library.models import MediaAsset

from . import services

COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
DATETIME_INPUT = forms.DateTimeInput(
    attrs={"type": "datetime-local", "class": "form-input-styled w-full"}, format="%Y-%m-%dT%H:%M"
)


def _input(**attrs):
    return forms.TextInput(attrs={"class": "form-input-styled w-full", **attrs})


class AssetChoiceForm(forms.Form):
    """Ad account, Pixel and Page, offered from what the Meta sign-in reaches.

    The choices are filled by the view from Meta's lists, so a value that is not
    in them is refused here as well as re-checked by ``services.choose_assets``.
    """

    ad_account_id = forms.ChoiceField(label=_("Ad account"))
    pixel_id = forms.ChoiceField(label=_("Pixel"), required=False)
    page_id = forms.ChoiceField(label=_("Facebook Page"), required=False)

    def __init__(self, *args, choices: services.AssetChoices, **kwargs):
        super().__init__(*args, **kwargs)
        account_field = self.fields["ad_account_id"]
        pixel_field = self.fields["pixel_id"]
        page_field = self.fields["page_id"]
        assert isinstance(account_field, forms.ChoiceField)
        assert isinstance(pixel_field, forms.ChoiceField)
        assert isinstance(page_field, forms.ChoiceField)
        account_field.choices = [
            (a.id, f"{a.name} · {a.currency}" + ("" if a.is_active else f" ({_('inactive')})"))
            for a in choices.accounts
        ]
        pixel_field.choices = [("", _("No Pixel"))] + [(p.id, p.name) for p in choices.pixels]
        page_field.choices = [("", _("No Page"))] + [(p.id, p.name) for p in choices.pages]


class CampaignCreateForm(forms.Form):
    name = forms.CharField(label=_("Campaign name"), max_length=200, widget=_input())
    objective = forms.ChoiceField(
        label=_("Objective"),
        choices=[(key, services.OBJECTIVES[key]) for key in services.CREATABLE_OBJECTIVES],
        widget=forms.RadioSelect,
        initial="OUTCOME_TRAFFIC",
    )
    daily_budget = forms.DecimalField(
        label=_("Daily budget"),
        min_value=Decimal("1"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-input-styled w-full", "step": "0.01", "min": "1"}),
    )
    start_time = forms.DateTimeField(label=_("Starts"), required=False, widget=DATETIME_INPUT)
    end_time = forms.DateTimeField(label=_("Ends"), required=False, widget=DATETIME_INPUT)
    countries = forms.CharField(
        label=_("Countries"),
        initial="BR",
        max_length=200,
        help_text=_("Two-letter codes separated by commas, e.g. BR, PT."),
        widget=_input(),
    )
    age_min = forms.IntegerField(
        label=_("Minimum age"), min_value=18, max_value=65, initial=18, widget=_input(type="number")
    )
    age_max = forms.IntegerField(
        label=_("Maximum age"), min_value=18, max_value=65, initial=65, widget=_input(type="number")
    )
    gender = forms.ChoiceField(
        label=_("Gender"),
        choices=[("all", _("Everyone")), ("1", _("Men")), ("2", _("Women"))],
        initial="all",
        widget=forms.Select(attrs={"class": "form-input-styled w-full"}),
    )
    image = forms.ModelChoiceField(
        label=_("Image"),
        queryset=MediaAsset.objects.none(),
        to_field_name="id",
        widget=forms.RadioSelect,
        error_messages={"invalid_choice": _("Choose one of the images of the gallery.")},
    )
    message = forms.CharField(
        label=_("Primary text"),
        max_length=2000,
        widget=forms.Textarea(attrs={"class": "form-input-styled w-full", "rows": 4}),
    )
    headline = forms.CharField(label=_("Headline"), max_length=255, widget=_input())
    link = forms.URLField(label=_("Destination link"), max_length=1000, assume_scheme="https", widget=_input())
    call_to_action = forms.ChoiceField(
        label=_("Button"),
        choices=list(services.CALLS_TO_ACTION.items()),
        initial="LEARN_MORE",
        widget=forms.Select(attrs={"class": "form-input-styled w-full"}),
    )

    def __init__(self, *args, workspace, connection, **kwargs):
        super().__init__(*args, **kwargs)
        self.connection = connection
        self.fields["image"].queryset = MediaAsset.objects.for_workspace_with_shared(
            workspace_id=workspace.id, organization_id=workspace.organization_id
        ).filter(media_type=MediaAsset.MediaType.IMAGE, processing_status=MediaAsset.ProcessingStatus.COMPLETED)
        currency = connection.currency or ""
        self.fields["daily_budget"].help_text = _("In %(currency)s, the ad account's currency.") % {
            "currency": currency
        }

    def clean_countries(self):
        codes = [c.strip().upper() for c in self.cleaned_data["countries"].split(",") if c.strip()]
        if not codes or not all(COUNTRY_RE.match(c) for c in codes):
            raise forms.ValidationError(_("Use two-letter country codes separated by commas, e.g. BR, PT."))
        return sorted(set(codes))

    def clean_link(self):
        link = self.cleaned_data["link"]
        if not link.lower().startswith(("https://", "http://")):
            raise forms.ValidationError(_("The link must start with https://."))
        return link

    def clean(self):
        data = super().clean()
        if data.get("age_min") and data.get("age_max") and data["age_min"] > data["age_max"]:
            self.add_error("age_max", _("The maximum age must be at least the minimum age."))
        start, end = data.get("start_time"), data.get("end_time")
        if end and end <= (start or timezone.now()):
            self.add_error("end_time", _("The end must be after the start."))
        if data.get("objective") in services.PIXEL_OBJECTIVES and not self.connection.pixel_id:
            self.add_error(
                "objective", _("Sales and lead campaigns need a Pixel. Choose one in the connection settings.")
            )
        return data

    def to_new_campaign(self, image_bytes: bytes) -> services.NewCampaign:
        data = self.cleaned_data
        return services.NewCampaign(
            name=data["name"],
            objective=data["objective"],
            daily_budget=data["daily_budget"],
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
            countries=data["countries"],
            age_min=data["age_min"],
            age_max=data["age_max"],
            genders=[] if data["gender"] == "all" else [int(data["gender"])],
            image_bytes=image_bytes,
            message=data["message"],
            headline=data["headline"],
            link=data["link"],
            call_to_action=data["call_to_action"],
        )


class CampaignRenameForm(forms.Form):
    name = forms.CharField(label=_("Campaign name"), max_length=200, widget=_input())


class AdSetEditForm(forms.Form):
    daily_budget = forms.DecimalField(
        label=_("Daily budget"),
        required=False,
        min_value=Decimal("1"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-input-styled w-full", "step": "0.01", "min": "1"}),
    )
    end_time = forms.DateTimeField(label=_("Ends"), required=False, widget=DATETIME_INPUT)

    def clean_end_time(self):
        end = self.cleaned_data.get("end_time")
        if end and end <= timezone.now():
            raise forms.ValidationError(_("The end must be in the future."))
        return end
