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

from . import catalog, defaults, triggers
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


@superadmin_required
@require_GET
def index(request):
    overrides = {row.key: row for row in SystemEmailOverride.objects.all()}
    groups: dict[str, list] = {}
    for entry in catalog.SYSTEM_EMAILS:
        groups.setdefault(str(entry.group), []).append({"entry": entry, "override": overrides.get(entry.key)})
    automated = AutomatedEmail.objects.annotate(sent=Count("sends")).order_by("-is_active", "name")
    return render(
        request,
        "email_center/staff_index.html",
        {"admin_active": "emails", "groups": groups, "automated": automated},
    )


@superadmin_required
@require_http_methods(["GET", "POST"])
def system_edit(request, key):
    entry = _entry_or_404(key)
    override = SystemEmailOverride.objects.filter(key=key).first()
    initial = None
    if override is None:
        # Start from what is being sent today, converted to Markdown.
        subject, body = defaults.default_version(entry)
        initial = {"subject": subject, "body_markdown": body}
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

    _text, default_html = render_email(defaults.code_template(key), entry.sample())
    return render(
        request,
        "email_center/staff_system_edit.html",
        {
            "admin_active": "emails",
            "entry": entry,
            "override": override,
            "form": form,
            "variables": catalog.all_variables(entry),
            "default_html": default_html,
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
            "variables": triggers.VARIABLES,
            "setup_steps": triggers.SETUP_STEPS,
            "sent_count": email.sends.count() if email else 0,
        },
    )


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
