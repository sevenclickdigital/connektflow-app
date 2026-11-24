from django import forms
from django.utils.translation import gettext_lazy as _

from . import catalog, markdown_mail, triggers
from .models import AutomatedEmail, SystemEmailOverride

_INPUT = "form-input-styled w-full"
_MARKDOWN = "form-input-styled w-full font-mono text-sm leading-6"


def _check_variables(form, allowed: set[str]) -> None:
    unknown = (
        markdown_mail.variables_used(form.cleaned_data.get("subject", ""), form.cleaned_data.get("body_markdown", ""))
        - allowed
    )
    if unknown:
        form.add_error(
            "body_markdown",
            _("These variables don't exist in this e-mail: %(names)s.")
            % {"names": ", ".join(f"{{{{ {name} }}}}" for name in sorted(unknown))},
        )


class SystemEmailForm(forms.ModelForm):
    class Meta:
        model = SystemEmailOverride
        fields = ["subject", "body_markdown", "is_active"]
        labels = {
            "subject": _("Subject"),
            "body_markdown": _("Text (Markdown)"),
            "is_active": _("Send this version instead of the default one"),
        }
        widgets = {
            "subject": forms.TextInput(attrs={"class": _INPUT}),
            "body_markdown": forms.Textarea(attrs={"class": _MARKDOWN, "rows": 18}),
        }

    def __init__(self, *args, entry, **kwargs):
        self.entry = entry
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        _check_variables(self, set(catalog.all_variables(self.entry)))
        return cleaned


class AutomatedEmailForm(forms.ModelForm):
    class Meta:
        model = AutomatedEmail
        fields = [
            "name",
            "subject",
            "body_markdown",
            "anchor",
            "delay_days",
            "connections",
            "posts",
            "tasks",
            "tools",
            "setup_steps_min",
            "setup_steps_max",
            "include_overdue",
            "is_active",
        ]
        labels = {
            "name": _("Name (only the team sees it)"),
            "subject": _("Subject"),
            "body_markdown": _("Text (Markdown)"),
            "anchor": _("Count the days from"),
            "delay_days": _("Days after"),
            "connections": _("Connected a social account"),
            "posts": _("Created a post"),
            "tasks": _("Created a task"),
            "tools": _("Used a tool (Radar, Automations, Ads, Contacts or Funnels)"),
            "setup_steps_min": _("Setup steps done: at least"),
            "setup_steps_max": _("Setup steps done: at most"),
            "include_overdue": _("Also send to people whose day passed before it was switched on"),
            "is_active": _("Active"),
        }
        widgets = {
            "name": forms.TextInput(attrs={"class": _INPUT}),
            "subject": forms.TextInput(attrs={"class": _INPUT}),
            "body_markdown": forms.Textarea(attrs={"class": _MARKDOWN, "rows": 18}),
            "anchor": forms.Select(attrs={"class": _INPUT}),
            "delay_days": forms.NumberInput(attrs={"class": _INPUT, "min": 0, "max": 365}),
            "connections": forms.Select(attrs={"class": _INPUT}),
            "posts": forms.Select(attrs={"class": _INPUT}),
            "tasks": forms.Select(attrs={"class": _INPUT}),
            "tools": forms.Select(attrs={"class": _INPUT}),
            "setup_steps_min": forms.NumberInput(attrs={"class": _INPUT, "min": 0, "max": triggers.SETUP_STEPS}),
            "setup_steps_max": forms.NumberInput(attrs={"class": _INPUT, "min": 0, "max": triggers.SETUP_STEPS}),
        }

    def clean_delay_days(self):
        days = self.cleaned_data["delay_days"]
        if days > 365:
            raise forms.ValidationError(_("Use at most 365 days."))
        return days

    def clean(self):
        cleaned = super().clean()
        low, high = cleaned.get("setup_steps_min"), cleaned.get("setup_steps_max")
        for name, value in (("setup_steps_min", low), ("setup_steps_max", high)):
            if value is not None and value > triggers.SETUP_STEPS:
                self.add_error(name, _("There are %(n)s setup steps.") % {"n": triggers.SETUP_STEPS})
        if low is not None and high is not None and low > high:
            self.add_error("setup_steps_max", _("The most can't be below the least."))
        _check_variables(self, set(triggers.VARIABLES))
        return cleaned

    def save(self, commit=True):
        email = super().save(commit=False)
        email.start_clock_if_switched_on(was_active="is_active" not in self.changed_data and email.is_active)
        if commit:
            email.save()
        return email
