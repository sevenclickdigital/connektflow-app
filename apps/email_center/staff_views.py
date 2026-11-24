"""The team's e-mail screens: system e-mails rewritten in Markdown, and automatic e-mails.

What is saved here reaches every customer, so every view is
``superadmin_required`` (``apps.accounts.decorators``).
"""

from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.decorators import superadmin_required
from apps.common.emails import email_context, render_email
from apps.common.htmx import toast_response

from . import catalog, defaults, markdown_mail, triggers
from .forms import AutomatedEmailForm, SystemEmailForm
from .models import AutomatedEmail, SystemEmailOverride
from .rendering import default_reason, render_message


def _entry_or_404(key):
    entry = catalog.get(key)
    if entry is None:
        raise Http404
    return entry


def _automated_sample(user) -> dict[str, str]:
    values = {name: "" for name in triggers.VARIABLES}
    common = email_context()
    values.update(
        name=user.greeting_name or user.email,
        email=user.email,
        organization_name="CAMILA AP",
        brand="Loja Centro",
        app_name=str(common.get("brand_name", "")),
        app_url=str(common.get("app_url", "")),
        connect_url=str(common.get("app_url", "")),
        compose_url=str(common.get("app_url", "")),
        tasks_url=str(common.get("app_url", "")),
        support_email=str(common.get("support_email", "")),
    )
    return values


def _variable_list(variables: dict) -> list[tuple[str, str]]:
    """``(name, label)`` of each variable, for the editor's chips and guide."""
    return [(name, str(getattr(variable, "label", variable))) for name, variable in variables.items()]


def _language(value) -> str:
    """``value`` when it is one of ``settings.LANGUAGES``, else the site's default language."""
    codes = defaults.language_codes()
    if value in codes:
        return value
    return settings.LANGUAGE_CODE if settings.LANGUAGE_CODE in codes else codes[0]


def _languages() -> list[dict]:
    """Each language of the versions, with its name and its short label (``PT``, ``EN``)."""
    names = dict(settings.LANGUAGES)
    return [
        {"code": code, "name": _(names[code]), "short": code.split("-")[0].upper()}
        for code in defaults.language_codes()
    ]


def _system_edit_url(key: str, language: str) -> str:
    return f"{reverse('email_center_staff:system_edit', kwargs={'key': key})}?{urlencode({'lang': language})}"


@superadmin_required
@require_GET
def index(request):
    defaults.ensure_versions()
    versions = {(row.key, row.language): row for row in SystemEmailOverride.objects.all()}
    languages = _languages()
    groups = [
        {
            "group": group,
            "items": [
                {
                    "entry": entry,
                    "versions": [
                        {**language, "row": versions.get((entry.key, language["code"]))} for language in languages
                    ],
                }
                for entry in catalog.SYSTEM_EMAILS
                if entry.group is group
            ],
        }
        for group in catalog.GROUPS
    ]
    automated = list(
        AutomatedEmail.objects.annotate(sent=Count("sends")).prefetch_related("texts").order_by("-is_active", "name")
    )
    return render(
        request,
        "email_center/staff_index.html",
        {
            "admin_active": "emails",
            "groups": [group for group in groups if group["items"]],
            "system_count": len(catalog.SYSTEM_EMAILS),
            "automated": automated,
            "active_count": sum(email.is_active for email in automated),
        },
    )


@superadmin_required
@require_http_methods(["GET", "POST"])
def system_edit(request, key):
    entry = _entry_or_404(key)
    language = _language(request.POST.get("language") or request.GET.get("lang"))
    defaults.ensure_versions([key])
    version = SystemEmailOverride.objects.filter(key=key, language=language).first()
    initial = None
    if version is None:
        # The default text could not be stored (logged by ensure_versions): start from it anyway.
        subject, body = defaults.default_in(entry, language)
        initial = {"subject": subject, "body_markdown": body}
    form = SystemEmailForm(request.POST or None, instance=version, initial=initial, entry=entry)
    if request.method == "POST" and form.is_valid():
        row = form.save(commit=False)
        row.key = key
        row.language = language
        row.is_default_text = False
        row.updated_by = request.user
        row.save()
        messages.success(
            request, _("Saved. This version is now sent.") if row.is_active else _("Saved, not in use yet.")
        )
        return redirect(_system_edit_url(key, language))

    # The default version as sent today in this language, with the sample data, to compare.
    sample = entry.sample()
    with translation.override(language):
        default_subject, _body = defaults.default_version(entry)
        _text, default_html = render_email(defaults.code_template(key), sample)
        sample_values = catalog.resolve(entry, email_context(**sample))
    languages = [{**item, "url": _system_edit_url(key, item["code"])} for item in _languages()]
    return render(
        request,
        "email_center/staff_system_edit.html",
        {
            "admin_active": "emails",
            "entry": entry,
            "override": version,
            "form": form,
            "language": language,
            "languages": languages,
            # The e-mail's own variables first: they are the ones it is about.
            "variables": _variable_list({**entry.variables, **catalog.COMMON_VARIABLES}),
            "default_html": default_html,
            "default_subject": markdown_mail.substitute_text(default_subject, sample_values),
        },
    )


@superadmin_required
@require_POST
def system_reset(request, key):
    entry = _entry_or_404(key)
    language = _language(request.POST.get("language"))
    subject, body = defaults.default_in(entry, language)
    SystemEmailOverride.objects.update_or_create(
        key=key,
        language=language,
        defaults={
            "subject": subject[:200],
            "body_markdown": body,
            "is_active": False,
            "is_default_text": True,
            "updated_by": request.user,
        },
    )
    messages.success(request, _("Back to the default version."))
    return redirect(_system_edit_url(key, language))


@superadmin_required
@require_http_methods(["GET", "POST"])
def automated_edit(request, email_id=None):
    email = get_object_or_404(AutomatedEmail.objects.prefetch_related("texts"), pk=email_id) if email_id else None
    form = AutomatedEmailForm(request.POST or None, instance=email)
    if request.method == "POST" and form.is_valid():
        form.instance.updated_by = request.user
        saved = form.save()
        messages.success(request, _("Automatic e-mail saved."))
        return redirect("email_center_staff:automated_edit", email_id=saved.pk)
    panes = form.language_panes()
    return render(
        request,
        "email_center/staff_automated_edit.html",
        {
            "admin_active": "emails",
            "email": email,
            "form": form,
            "variables": _variable_list(triggers.VARIABLES),
            "panes": panes,
            "start_language": next((pane["code"] for pane in panes if pane["has_errors"]), form.default_language),
            "default_language": form.default_language,
            "default_language_name": _(dict(settings.LANGUAGES)[form.default_language]),
            "setup_steps": triggers.SETUP_STEPS,
            "sent_count": email.sends.count() if email else 0,
        },
    )


@superadmin_required
@require_POST
def automated_toggle(request, email_id):
    email = get_object_or_404(AutomatedEmail, pk=email_id)
    was_active = email.is_active
    email.is_active = not was_active
    email.start_clock_if_switched_on(was_active)
    email.updated_by = request.user
    email.save(update_fields=["is_active", "activated_at", "updated_by", "updated_at"])
    messages.success(
        request, _("Automatic e-mail switched on.") if email.is_active else _("Automatic e-mail switched off.")
    )
    return redirect("email_center_staff:index")


@superadmin_required
@require_POST
def automated_delete(request, email_id):
    get_object_or_404(AutomatedEmail, pk=email_id).delete()
    messages.success(request, _("Automatic e-mail deleted."))
    return redirect("email_center_staff:index")


def _draft(request):
    """``(subject, text, html)`` of the e-mail being edited, from the form as it is now.

    It is rendered in the language being edited. An automatic e-mail carries
    every language's text; a language left empty shows what its readers get,
    the default language's text.
    """
    key = request.POST.get("key", "")
    language = _language(request.POST.get("language"))
    if not key:
        subject = request.POST.get(f"subject__{language}", "").strip()
        body = request.POST.get(f"body__{language}", "")
        if not subject and not body.strip():
            language = _language(None)
            subject = request.POST.get(f"subject__{language}", "")
            body = request.POST.get(f"body__{language}", "")
        with translation.override(language):
            return render_message(
                subject=subject, body_markdown=body, values=_automated_sample(request.user), context={}
            )
    subject = request.POST.get("subject", "")
    body = request.POST.get("body_markdown", "")
    entry = _entry_or_404(key)
    sample = entry.sample()
    with translation.override(language):
        return render_message(
            subject=subject,
            body_markdown=body,
            values=catalog.resolve(entry, email_context(**sample)),
            context={},
            reason_html=default_reason(key, sample),
        )


@superadmin_required
@require_POST
def preview(request):
    subject, _text, html = _draft(request)
    return HttpResponse(render_to_string("email_center/partials/_preview.html", {"subject": subject, "html": html}))


@superadmin_required
@require_POST
def send_test(request):
    from django.core.mail import EmailMultiAlternatives

    from apps.common.mail import EmailNotSentError, send_or_raise

    subject, text, html = _draft(request)
    message = EmailMultiAlternatives(
        subject=_("[Test] %(subject)s") % {"subject": subject}, body=text, to=[request.user.email]
    )
    message.attach_alternative(html, "text/html")
    try:
        send_or_raise(message)
    except EmailNotSentError:
        return toast_response(tone="error", title=_("The test was not sent: the e-mail sending limit was reached."))
    return toast_response(tone="success", title=_("Test sent to %(email)s.") % {"email": request.user.email})
