"""The team's e-mail screens: system e-mails rewritten in Markdown, and automatic e-mails.

What is saved here reaches every customer, so every view is
``superadmin_required`` (``apps.accounts.decorators``).
"""

from django.contrib import messages
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
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


@superadmin_required
@require_GET
def index(request):
    overrides = {row.key: row for row in SystemEmailOverride.objects.all()}
    groups = [
        {
            "group": group,
            "items": [
                {"entry": entry, "override": overrides.get(entry.key)}
                for entry in catalog.SYSTEM_EMAILS
                if entry.group is group
            ],
        }
        for group in catalog.GROUPS
    ]
    automated = list(AutomatedEmail.objects.annotate(sent=Count("sends")).order_by("-is_active", "name"))
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
    override = SystemEmailOverride.objects.filter(key=key).first()
    default_subject, default_body = defaults.default_version(entry)
    # Without an edited version, start from what is being sent today, converted to Markdown.
    initial = None if override else {"subject": default_subject, "body_markdown": default_body}
    form = SystemEmailForm(request.POST or None, instance=override, initial=initial, entry=entry)
    if request.method == "POST" and form.is_valid():
        row = form.save(commit=False)
        row.key = key
        row.updated_by = request.user
        row.save()
        messages.success(
            request, _("Saved. This version is now sent.") if row.is_active else _("Saved, not in use yet.")
        )
        return redirect("email_center_staff:system_edit", key=key)

    # The default version as sent today, with the sample data, to compare.
    sample = entry.sample()
    _text, default_html = render_email(defaults.code_template(key), sample)
    sample_values = catalog.resolve(entry, email_context(**sample))
    return render(
        request,
        "email_center/staff_system_edit.html",
        {
            "admin_active": "emails",
            "entry": entry,
            "override": override,
            "form": form,
            # The e-mail's own variables first: they are the ones it is about.
            "variables": _variable_list({**entry.variables, **catalog.COMMON_VARIABLES}),
            "default_html": default_html,
            "default_subject": markdown_mail.substitute_text(default_subject, sample_values),
        },
    )


@superadmin_required
@require_POST
def system_reset(request, key):
    _entry_or_404(key)
    SystemEmailOverride.objects.filter(key=key).delete()
    messages.success(request, _("Back to the default version."))
    return redirect("email_center_staff:system_edit", key=key)


@superadmin_required
@require_http_methods(["GET", "POST"])
def automated_edit(request, email_id=None):
    email = get_object_or_404(AutomatedEmail, pk=email_id) if email_id else None
    form = AutomatedEmailForm(
        request.POST or None,
        instance=email,
        initial=None if email else {"body_markdown": "# \n\n" + _("Hi {{ name }},") + "\n\n"},
    )
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.updated_by = request.user
        saved.save()
        messages.success(request, _("Automatic e-mail saved."))
        return redirect("email_center_staff:automated_edit", email_id=saved.pk)
    return render(
        request,
        "email_center/staff_automated_edit.html",
        {
            "admin_active": "emails",
            "email": email,
            "form": form,
            "variables": _variable_list(triggers.VARIABLES),
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
    """``(subject, text, html)`` of the e-mail being edited, from the form as it is now."""
    subject = request.POST.get("subject", "")
    body = request.POST.get("body_markdown", "")
    key = request.POST.get("key", "")
    reason_html = ""
    if key:
        entry = _entry_or_404(key)
        sample = entry.sample()
        values = catalog.resolve(entry, email_context(**sample))
        reason_html = default_reason(key, sample)
    else:
        values = _automated_sample(request.user)
    return render_message(subject=subject, body_markdown=body, values=values, context={}, reason_html=reason_html)


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
