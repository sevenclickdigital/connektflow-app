"""Contacts: the list with its filters, the contact form, and CSV in and out."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.common.htmx import toast_response
from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount

from . import services
from .forms import BRAZIL_STATES, MAX_NOTES_LENGTH, ContactForm, clean_photo, parse_channels
from .models import ChannelKind, Contact

logger = logging.getLogger(__name__)

FILTER_KEYS = ("q", "channel", "status", "source", "funnel", "tag", "segment", "engagement", "last")

CONTACTS_CHANGED = {"contactsChanged": True}


def _filters(request) -> dict[str, str]:
    return {key: request.GET.get(key, "").strip() for key in FILTER_KEYS if request.GET.get(key, "").strip()}


@login_required
@require_permission("use_inbox")
@require_GET
def contact_list(request, workspace_id):
    filters = _filters(request)
    qs = (
        services.filter_contacts(workspace_id, filters)
        .prefetch_related("channels")
        .order_by("-last_contact_at", "name")
    )
    page = Paginator(qs, services.PAGE_SIZE).get_page(request.GET.get("page"))
    facets = services.facet_values(workspace_id)
    context = {
        "workspace_id": workspace_id,
        "page": page,
        "page_numbers": page.paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1),
        "filters": filters,
        "filter_query": urlencode(filters),
        "has_filters": bool(filters),
        # The channel pills (calendar/partials/_platform_pills.html), as on the calendar and the inbox.
        "social_accounts": SocialAccount.objects.filter(
            workspace_id=workspace_id, connection_status=SocialAccount.ConnectionStatus.CONNECTED
        ).order_by("platform"),
        "active_filters": {"platforms": [filters["channel"]] if filters.get("channel") else []},
        "facets": facets,
        "status_choices": Contact.Status.choices,
        "source_choices": Contact.Source.choices,
        "engagement_choices": Contact.Engagement.choices,
        "last_choices": [
            ("7", _("Last 7 days")),
            ("30", _("Last 30 days")),
            ("90", _("Last 90 days")),
            ("older", _("Over 90 days ago")),
            ("never", _("Never")),
        ],
    }
    if request.htmx and request.htmx.target == "contacts-results":
        return render(request, "contacts/partials/_results.html", context)
    context["stats"] = services.stats(workspace_id)
    return render(request, "contacts/list.html", context)


def _channel_placeholder(kind: str) -> str:
    if kind in (ChannelKind.WHATSAPP, ChannelKind.PHONE):
        return "+55 48 99999-9999"
    if kind == ChannelKind.EMAIL:
        return _("name@company.com")
    if kind in services.HANDLE_KINDS:
        return _("@username")
    return ""


def _form_page(request, workspace_id, contact: Contact | None):
    """Create (``contact`` None) or edit a contact."""
    is_new = contact is None
    instance = contact or Contact(workspace_id=workspace_id)
    channel_errors: list[str] = []
    photo_error = ""
    if request.method == "POST":
        form = ContactForm(request.POST, instance=instance)
        channels, channel_errors = parse_channels(request.POST)
        upload = request.FILES.get("photo")
        if upload:
            photo_error = clean_photo(upload)
        if form.is_valid() and not channel_errors and not photo_error:
            with transaction.atomic():
                saved = form.save(commit=False)
                if is_new:
                    saved.created_by = request.user
                if upload:
                    if saved.photo:
                        saved.photo.delete(save=False)
                    saved.photo = upload
                elif request.POST.get("remove_photo") == "1" and saved.photo:
                    saved.photo.delete(save=False)
                    saved.photo = ""
                saved.save()
                services.save_channels(saved, channels)
                services.refresh_interactions(saved)
            if is_new:
                messages.success(request, _("Contact created."))
            else:
                messages.success(request, _("Contact saved."))
            return redirect("contacts:list", workspace_id=workspace_id)
        posted_channels = [{"kind": c.kind, "value": c.value, "primary": c.is_primary} for c in channels]
    else:
        form = ContactForm(instance=instance)
        posted_channels = (
            [{"kind": c.kind, "value": c.value, "primary": c.is_primary} for c in instance.channels.all()]
            if not is_new
            else []
        )

    interactions = []
    if not is_new:
        interactions = list(
            services.interactions_queryset(instance).select_related("social_account").order_by("-received_at")[:20]
        )
    facets = services.facet_values(workspace_id)
    return render(
        request,
        "contacts/form.html",
        {
            "workspace_id": workspace_id,
            "contact": instance,
            "is_new": is_new,
            "form": form,
            "channel_errors": channel_errors,
            "photo_error": photo_error,
            "channels_data": posted_channels,
            "channel_kinds": [
                {"value": k, "label": str(label), "placeholder": _channel_placeholder(k)}
                for k, label in ChannelKind.choices
            ],
            "tags_data": form["tags"].value() if request.method == "POST" else form.initial.get("tags", ""),
            "facets": facets,
            "states": BRAZIL_STATES,
            "max_notes": MAX_NOTES_LENGTH,
            "interactions": interactions,
            "computed_engagement": Contact.Engagement(Contact.level_for(instance.interactions_90d)).label,
        },
    )


@login_required
@require_permission("use_inbox")
@require_http_methods(["GET", "POST"])
def contact_create(request, workspace_id):
    return _form_page(request, workspace_id, None)


@login_required
@require_permission("use_inbox")
@require_http_methods(["GET", "POST"])
def contact_edit(request, workspace_id, contact_id):
    contact = get_object_or_404(Contact, id=contact_id, workspace_id=workspace_id)
    return _form_page(request, workspace_id, contact)


@login_required
@require_permission("use_inbox")
@require_POST
def contact_delete(request, workspace_id, contact_id):
    contact = get_object_or_404(Contact, id=contact_id, workspace_id=workspace_id)
    _delete_contacts(Contact.objects.filter(pk=contact.pk))
    if request.htmx:
        return toast_response(tone="success", title=_("Contact deleted."), events=CONTACTS_CHANGED)
    messages.success(request, _("Contact deleted."))
    return redirect("contacts:list", workspace_id=workspace_id)


@login_required
@require_permission("use_inbox")
@require_POST
def contact_bulk_delete(request, workspace_id):
    ids = request.POST.getlist("ids")[:500]
    contacts = Contact.objects.for_workspace(workspace_id).filter(id__in=_valid_uuids(ids))
    count = _delete_contacts(contacts)
    if not count:
        return toast_response(tone="warn", title=_("Select at least one contact."))
    title = ngettext("%(count)s contact deleted.", "%(count)s contacts deleted.", count) % {"count": count}
    return toast_response(tone="success", title=title, events=CONTACTS_CHANGED)


@login_required
@require_permission("use_inbox")
@require_POST
def contact_merge(request, workspace_id):
    ids = _valid_uuids(request.POST.getlist("ids")[: services.MAX_MERGE])
    keep = request.POST.get("keep", "")
    if len(ids) < 2 or keep not in ids:
        return toast_response(tone="warn", title=_("Select at least two contacts and the one to keep."))
    try:
        kept = services.merge_contacts(workspace_id, keep, ids)
    except Contact.DoesNotExist:
        return toast_response(
            tone="error", title=_("Those contacts could not be found. Reload the page and try again.")
        )
    title = ngettext(
        "%(count)s contact merged into %(name)s.", "%(count)s contacts merged into %(name)s.", len(ids) - 1
    ) % {"count": len(ids) - 1, "name": kept.name}
    return toast_response(tone="success", title=title, events=CONTACTS_CHANGED)


def _valid_uuids(values) -> list[str]:
    import uuid

    valid = []
    for value in values:
        try:
            valid.append(str(uuid.UUID(str(value))))
        except ValueError:
            continue
    return valid


def _delete_contacts(contacts) -> int:
    count = 0
    for contact in contacts:
        if contact.photo:
            contact.photo.delete(save=False)
        contact.delete()
        count += 1
    return count


@login_required
@require_permission("use_inbox")
@require_POST
def contact_set_funnel(request, workspace_id, contact_id):
    contact = get_object_or_404(Contact, id=contact_id, workspace_id=workspace_id)
    contact.funnel = request.POST.get("funnel", "").strip()[:100]
    contact.save(update_fields=["funnel", "updated_at"])
    if contact.funnel:
        title = _("Moved to the %(funnel)s funnel.") % {"funnel": contact.funnel}
    else:
        title = _("Removed from the funnel.")
    return toast_response(tone="success", title=title)


@login_required
@require_permission("use_inbox")
@ratelimit(key="user", rate="10/m", method="GET", block=True)
@require_GET
def contact_export(request, workspace_id):
    contacts = services.filter_contacts(workspace_id, _filters(request)).order_by("name")
    response = HttpResponse(services.export_csv(contacts), content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="contacts-{stamp}.csv"'
    return response


@login_required
@require_permission("use_inbox")
@ratelimit(key="user", rate="5/m", method="POST", block=True)
@require_POST
def contact_import(request, workspace_id):
    upload = request.FILES.get("csv_file")
    back = reverse("contacts:list", kwargs={"workspace_id": workspace_id})
    if not upload:
        messages.error(request, _("Choose a CSV file to import."))
        return redirect(back)
    if upload.size > services.MAX_IMPORT_BYTES:
        messages.error(request, _("The file must be under 2 MB."))
        return redirect(back)
    result = services.import_csv(workspace_id, upload.read(), user=request.user)
    for error in result.errors:
        messages.error(request, error)
    if result.created or result.skipped:
        messages.success(
            request,
            _("Import finished: %(created)s created, %(skipped)s skipped (no name or already in your contacts).")
            % {"created": result.created, "skipped": result.skipped},
        )
    return redirect(back)
