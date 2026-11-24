from django import forms
from django.conf import settings
from django.db import transaction
from django.utils import translation
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.common.emails import supported_language

from . import catalog, defaults, markdown_mail, triggers
from .models import AutomatedEmail, AutomatedEmailText, SystemEmailOverride

_INPUT = "form-input-styled w-full"
_MARKDOWN = "form-input-styled w-full font-mono text-sm leading-6"


def _check_variables(form, allowed: set[str], subject_field="subject", body_field="body_markdown") -> None:
    unknown = (
        markdown_mail.variables_used(
            form.cleaned_data.get(subject_field) or "", form.cleaned_data.get(body_field) or ""
        )
        - allowed
    )
    if unknown:
        form.add_error(
            body_field,
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


def _subject_field(language: str) -> str:
    return f"subject__{language}"


def _body_field(language: str) -> str:
    return f"body__{language}"


class AutomatedEmailForm(forms.ModelForm):
    """The trigger of an automatic e-mail and its text in each language of ``settings.LANGUAGES``.

    The default language's subject and text are required; another language's
    are optional (left empty, its readers get the default text) but go
    together.
    """

    class Meta:
        model = AutomatedEmail
        fields = [
            "name",
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
            "anchor": forms.Select(attrs={"class": _INPUT}),
            "delay_days": forms.NumberInput(attrs={"class": _INPUT, "min": 0, "max": 365}),
            "connections": forms.Select(attrs={"class": _INPUT}),
            "posts": forms.Select(attrs={"class": _INPUT}),
            "tasks": forms.Select(attrs={"class": _INPUT}),
            "tools": forms.Select(attrs={"class": _INPUT}),
            "setup_steps_min": forms.NumberInput(attrs={"class": _INPUT, "min": 0, "max": triggers.SETUP_STEPS}),
            "setup_steps_max": forms.NumberInput(attrs={"class": _INPUT, "min": 0, "max": triggers.SETUP_STEPS}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_language = supported_language(settings.LANGUAGE_CODE)
        texts = {} if self.instance._state.adding else {text.language: text for text in self.instance.texts.all()}
        for language in defaults.language_codes():
            text = texts.get(language)
            required = language == self.default_language
            self.fields[_subject_field(language)] = forms.CharField(
                label=_("Subject"),
                max_length=200,
                required=required,
                initial=text.subject if text else "",
                widget=forms.TextInput(attrs={"class": _INPUT}),
            )
            self.fields[_body_field(language)] = forms.CharField(
                label=_("Text (Markdown)"),
                required=required,
                strip=False,
                initial=text.body_markdown if text else _starting_text(language) if self.instance._state.adding else "",
                widget=forms.Textarea(attrs={"class": _MARKDOWN, "rows": 18}),
            )

    def language_panes(self) -> list[dict]:
        """Each language's subject and text, for the editor's tabs."""
        names = dict(settings.LANGUAGES)
        return [
            {
                "code": language,
                "name": _(names[language]),
                "short": language.split("-")[0].upper(),
                "subject": self[_subject_field(language)],
                "body": self[_body_field(language)],
                "has_errors": bool(self[_subject_field(language)].errors or self[_body_field(language)].errors),
            }
            for language in defaults.language_codes()
        ]

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
        for language in defaults.language_codes():
            subject_field, body_field = _subject_field(language), _body_field(language)
            subject = (cleaned.get(subject_field) or "").strip()
            written = _has_text(cleaned.get(body_field) or "", language)
            if language != self.default_language and (subject or written) and not (subject and written):
                self.add_error(
                    body_field if subject else subject_field, _("Write both the subject and the text, or neither.")
                )
            _check_variables(self, set(triggers.VARIABLES), subject_field, body_field)
        return cleaned

    def save(self, commit=True):
        """Save the e-mail and its texts: an emptied language's text is removed."""
        email = super().save(commit=False)
        email.start_clock_if_switched_on(was_active="is_active" not in self.changed_data and email.is_active)
        if not commit:
            return email
        with transaction.atomic():
            email.save()
            for language in defaults.language_codes():
                subject = (self.cleaned_data.get(_subject_field(language)) or "").strip()
                body = self.cleaned_data.get(_body_field(language)) or ""
                if subject and _has_text(body, language):
                    AutomatedEmailText.objects.update_or_create(
                        automated_email=email, language=language, defaults={"subject": subject, "body_markdown": body}
                    )
                else:
                    AutomatedEmailText.objects.filter(automated_email=email, language=language).delete()
        return email


def _starting_text(language: str) -> str:
    """What a new e-mail's text starts with in ``language``: an empty title and the greeting."""
    with translation.override(language):
        return "# \n\n" + gettext("Hi {{ name }},") + "\n\n"


def _has_text(body: str, language: str) -> bool:
    """Whether ``body`` was written: not empty, and not the untouched starting text."""
    return bool(body.strip()) and body.strip() != _starting_text(language).strip()
