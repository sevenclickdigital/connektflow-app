from django import forms

from apps.organizations.models import Organization


class AboutBusinessForm(forms.ModelForm):
    """The "about your business" step of a new account. Every answer is optional."""

    goals = forms.MultipleChoiceField(choices=Organization.Goal.choices, required=False)

    class Meta:
        model = Organization
        fields = ["business_activity", "team_size", "referral_source", "goals"]
