from django import forms
from django.utils.translation import gettext_lazy as _

from apps.common.validators import clean_display_name
from apps.organizations.models import Organization


class AboutBusinessForm(forms.ModelForm):
    """The "about your business" step of a new account. Every question is answered.

    The organization is named here: its name is asked after the activity
    (Organization.name_question) and replaces the one it was created with.
    One more question follows the activity (Organization.extra_question):
    clients served or segment, required for that activity; the answer to
    the other one is cleared. Whoever found us "somewhere else" may say
    where; the text is kept only with that source.
    """

    organization_name = forms.CharField(max_length=100)

    class Meta:
        model = Organization
        fields = [
            "business_activity",
            "team_size",
            "referral_source",
            "referral_source_other",
            "client_count",
            "business_segment",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("business_activity", "team_size", "referral_source"):
            self.fields[name].required = True
        if not self.instance.is_unnamed:
            self.fields["organization_name"].initial = self.instance.name

    def clean_organization_name(self):
        name = clean_display_name(self.cleaned_data["organization_name"])
        if not name or name == Organization.DEFAULT_NAME:
            raise forms.ValidationError(_("Enter a name."))
        return name

    def clean(self):
        cleaned = super().clean()
        question = Organization.extra_question(cleaned.get("business_activity"))
        asked = question[0] if question else None
        for field in ("client_count", "business_segment"):
            if field != asked:
                cleaned[field] = ""
            elif not cleaned.get(field):
                self.add_error(field, _("Answer this question."))
        typed = cleaned.get("referral_source") == Organization.Source.OTHER
        cleaned["referral_source_other"] = clean_display_name(cleaned.get("referral_source_other", "")) if typed else ""
        return cleaned

    def save(self, commit=True):
        self.instance.name = self.cleaned_data["organization_name"]
        return super().save(commit=commit)
