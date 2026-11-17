"""Forms for the Post Composer."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.common.validators import normalize_tags, parse_and_truncate_tag_string

from . import idea_icons, services
from .models import ContentCategory, Idea, IdeaTemplate, IdeaTemplateVariation, Post, PostTemplate


class IdeaForm(forms.ModelForm):
    """Form for creating and editing ideas on the Kanban board."""

    class Meta:
        model = Idea
        fields = ["title", "description", "tags"]

    def clean_tags(self):
        tags = self.cleaned_data.get("tags") or []
        if isinstance(tags, str):
            return parse_and_truncate_tag_string(tags)
        try:
            return normalize_tags(tags)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e


class PostForm(forms.ModelForm):
    """Form for creating and editing posts in the composer."""

    # Platform selection comes from POST data as a list of social_account IDs
    selected_accounts = forms.CharField(
        widget=forms.HiddenInput(),
        required=False,
        help_text="Comma-separated list of SocialAccount UUIDs.",
    )

    scheduled_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    scheduled_time = forms.TimeField(
        required=False,
        widget=forms.TimeInput(attrs={"type": "time"}),
    )

    # Override JSONField's auto-generated form field - the frontend sends tags
    # as a comma-separated string, not JSON.  clean_tags() converts to a list.
    tags = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Post
        fields = ["title", "caption", "first_comment", "internal_notes", "tags", "category"]
        widgets = {
            "caption": forms.Textarea(
                attrs={
                    "rows": 6,
                    "placeholder": _("Write your post caption..."),
                    "class": "form-input w-full",
                    "x-model": "caption",
                    "@input.debounce.500ms": "updatePreview()",
                }
            ),
            "first_comment": forms.Textarea(
                attrs={
                    "rows": 2,
                    "placeholder": _("First comment (optional)..."),
                    "class": "form-input w-full",
                }
            ),
            "internal_notes": forms.Textarea(
                attrs={
                    "rows": 2,
                    "placeholder": _("Internal notes (not visible to clients)..."),
                    "class": "form-input w-full",
                }
            ),
        }

    def clean_tags(self):
        tags = self.cleaned_data.get("tags") or []
        if isinstance(tags, str):
            return parse_and_truncate_tag_string(tags)
        try:
            return normalize_tags(tags)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e


class ContentCategoryForm(forms.ModelForm):
    """Form for creating/editing content categories."""

    class Meta:
        model = ContentCategory
        fields = ["name", "color"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "placeholder": _("e.g., Educational, Promotional..."),
                    "class": "form-input w-full",
                }
            ),
            "color": forms.TextInput(
                attrs={
                    "type": "color",
                    "class": "w-10 h-10 rounded-lg border border-neutral-300 cursor-pointer p-0.5",
                }
            ),
        }


class PostTemplateForm(forms.ModelForm):
    """Form for creating/editing post templates."""

    class Meta:
        model = PostTemplate
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "placeholder": _("Template name..."),
                    "class": "form-input w-full",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "rows": 2,
                    "placeholder": _("Optional description..."),
                    "class": "form-input w-full",
                }
            ),
        }


class PlatformOverrideForm(forms.Form):
    """Form for per-platform caption/media overrides."""

    social_account_id = forms.UUIDField(widget=forms.HiddenInput())
    platform_specific_caption = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": _("Custom caption for this platform (leave empty to use shared caption)..."),
                "class": "form-input w-full text-sm",
            }
        ),
    )
    platform_specific_first_comment = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Custom first comment..."),
                "class": "form-input w-full text-sm",
            }
        ),
    )


def _checkboxes(choices, label):
    return forms.MultipleChoiceField(choices=choices, required=False, label=label, widget=forms.CheckboxSelectMultiple)


class IdeaTemplateForm(forms.ModelForm):
    """The operator's template form. The icon comes from the idea icon library
    (``idea_icons``); none picked means the category's."""

    formats = _checkboxes(IdeaTemplate.Format.choices, _("Format"))
    goals = _checkboxes(IdeaTemplate.Goal.choices, _("Objective"))
    #: Each network with the formats the template suits on it, as
    #: "<network>:<format>" values (the composer's Feed / Story / Reels).
    targets = forms.MultipleChoiceField(required=False, label=_("Networks"), widget=forms.MultipleHiddenInput)
    # A value a network does not offer is dropped on save
    # (services.network_formats_from_targets), not a reason to refuse the form.
    targets.valid_value = lambda value: True  # type: ignore[method-assign]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.network_options = services.template_network_options()
        self.fields["targets"].choices = [
            (option["value"], option["short"]) for network in self.network_options for option in network["formats"]
        ]
        if self.instance.pk and not self.is_bound:
            self.initial["targets"] = [
                f"{network}:{fmt}"
                for network, formats in (self.instance.network_formats or {}).items()
                for fmt in formats
            ]

    def initial_targets(self) -> list[str]:
        """The values ticked when the form shows, for the network cards."""
        if self.is_bound:
            return self.data.getlist("targets") if hasattr(self.data, "getlist") else []
        return list(self.initial.get("targets", []))

    def save(self, commit=True):
        template = super().save(commit=False)
        template.network_formats = services.network_formats_from_targets(self.cleaned_data.get("targets"))
        template.networks = list(template.network_formats)
        if commit:
            template.save()
        return template

    def clean_icon(self):
        """Only a slug of the library is kept; anything else means "the category's"."""
        value = (self.cleaned_data.get("icon") or "").strip()
        return value if value in idea_icons.ICONS else ""

    class Meta:
        model = IdeaTemplate
        fields = [
            "icon",
            "title",
            "description",
            "details",
            "body",
            "category",
            "image",
            "formats",
            "goals",
            "ideal_for",
            "is_active",
        ]
        labels = {
            "icon": _("Icon"),
            "title": _("Title"),
            "description": _("Description"),
            "body": _("Text of the template"),
            "category": _("Category"),
            "is_active": _("Shown to customers"),
            "details": _("Detailed description"),
            "image": _("Cover image"),
            "ideal_for": _("Ideal for"),
        }
        help_texts = {
            "description": _("One or two lines on the card, saying what the idea is."),
            "body": _("What opens in the composer. Mark the parts to fill in with double braces, like {{topic}}."),
            "details": _("Shown in the template's panel. Without it, the description is shown."),
            "image": _("The top of the card. Without one, the category's icon is shown."),
            "ideal_for": _("For example: tips, recommendations, lists."),
        }
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-input-styled w-full"}),
            "description": forms.Textarea(attrs={"class": "form-input-styled w-full", "rows": 2}),
            "body": forms.Textarea(attrs={"class": "form-input-styled w-full", "rows": 10}),
            "category": forms.Select(attrs={"class": "form-input-styled w-full"}),
            "details": forms.Textarea(attrs={"class": "form-input-styled w-full", "rows": 3}),
            "ideal_for": forms.TextInput(attrs={"class": "form-input-styled w-full"}),
        }


class IdeaTemplateVariationForm(forms.ModelForm):
    class Meta:
        model = IdeaTemplateVariation
        fields = ["title", "body"]
        labels = {"title": _("Name of the version"), "body": _("Text")}
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-input-styled w-full"}),
            "body": forms.Textarea(attrs={"class": "form-input-styled w-full", "rows": 5}),
        }


#: The other versions of a template's text, under its own.
VariationFormSet: type[forms.BaseInlineFormSet] = forms.inlineformset_factory(
    IdeaTemplate, IdeaTemplateVariation, form=IdeaTemplateVariationForm, extra=1, can_delete=True
)


class TemplateSubmissionForm(IdeaTemplateForm):
    """A customer's template idea: the operator's form without "shown to
    customers", which is the operator's approval."""

    #: The largest cover a customer may send.
    MAX_IMAGE_BYTES = 5 * 1024 * 1024

    class Meta(IdeaTemplateForm.Meta):
        fields = [
            "icon",
            "title",
            "description",
            "details",
            "body",
            "category",
            "image",
            "formats",
            "goals",
            "ideal_for",
        ]

    def clean_body(self):
        body = self.cleaned_data.get("body", "").strip()
        if not body:
            raise forms.ValidationError(_("Write the text of the template."))
        return body

    def clean_image(self):
        image = self.cleaned_data.get("image")
        if image and getattr(image, "size", 0) > self.MAX_IMAGE_BYTES:
            raise forms.ValidationError(_("The cover can have at most 5 MB."))
        return image
