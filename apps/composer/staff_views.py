"""The operator's shelf of idea templates: the starting points every brand
sees in the create page's Templates tab.

Access is ``_staff_required``, borrowed from ``apps.billing.views`` like the
other screens of the administrative area.
"""

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.billing.views import _staff_required

from . import idea_icons
from .forms import IdeaTemplateForm, VariationFormSet
from .models import IdeaTemplate
from .services import review_template


@_staff_required
@require_GET
def template_list(request):
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    # "Sent by customers" lists the ones waiting for approval; the shelf, the rest.
    sent = request.GET.get("tab") == "sent"
    pending = IdeaTemplate.objects.filter(review_status=IdeaTemplate.Review.PENDING)
    templates = (
        pending.select_related("submitted_by", "submitted_workspace")
        if sent
        else IdeaTemplate.objects.exclude(review_status=IdeaTemplate.Review.PENDING)
    )
    if query:
        templates = templates.filter(Q(title__icontains=query) | Q(description__icontains=query))
    if category in IdeaTemplate.Category.values:
        templates = templates.filter(category=category)
    listed = pending if sent else IdeaTemplate.objects.exclude(review_status=IdeaTemplate.Review.PENDING)
    counts = dict(listed.values_list("category").annotate(total=Count("id")))
    return render(
        request,
        "composer/staff_idea_templates.html",
        {
            "admin_active": "idea_templates",
            "templates": templates,
            "sent": sent,
            "pending_count": pending.count(),
            "query": query,
            "category": category,
            "categories": [
                {"slug": slug, "label": label, "count": counts.get(slug, 0)}
                for slug, label in IdeaTemplate.Category.choices
            ],
            "total": listed.count(),
            "hidden": 0 if sent else listed.filter(is_active=False).count(),
        },
    )


@_staff_required
@require_http_methods(["GET", "POST"])
def template_edit(request, template_id=None):
    """Create a template (no id) or change one."""
    template = get_object_or_404(IdeaTemplate, id=template_id) if template_id else None
    data = request.POST if request.method == "POST" else None
    files = request.FILES if request.method == "POST" else None
    form = IdeaTemplateForm(data, files, instance=template)
    variations = VariationFormSet(data, instance=template or IdeaTemplate(), prefix="variations")
    if request.method == "POST" and form.is_valid() and variations.is_valid():
        with transaction.atomic():
            saved = form.save(commit=False)
            # Ticking "shown to customers" on a sent template approves it.
            if saved.is_active:
                saved.review_status = IdeaTemplate.Review.APPROVED
            saved.save()
            form.save_m2m()
            variations.instance = saved
            # A new version goes after the ones already there.
            for offset, item in enumerate(variations.save(commit=False)):
                if item.position == 0 and item.pk is None:
                    item.position = 10_000 + offset
                item.save()
            for item in variations.deleted_objects:
                item.delete()
            # The kept ones in the order they were listed.
            for position, item in enumerate(saved.variations.all()):
                if item.position != position:
                    item.position = position
                    item.save(update_fields=["position"])
        messages.success(request, _("Template saved."))
        return redirect("composer_staff:idea_templates")
    return render(
        request,
        "composer/staff_idea_template_form.html",
        {
            "admin_active": "idea_templates",
            "form": form,
            "variations": variations,
            "template": template,
            "icon_choices": idea_icons.icon_library(),
        },
    )


@_staff_required
@require_POST
def template_toggle(request, template_id):
    template = get_object_or_404(IdeaTemplate, id=template_id)
    template.is_active = not template.is_active
    # Showing a sent or rejected template is approving it.
    if template.is_active:
        template.review_status = IdeaTemplate.Review.APPROVED
    template.save(update_fields=["is_active", "review_status", "updated_at"])
    return redirect("composer_staff:idea_templates")


@_staff_required
@require_POST
def template_review(request, template_id):
    """Approve a template a customer sent (it goes on every shelf) or reject it."""
    template = get_object_or_404(IdeaTemplate, id=template_id, review_status=IdeaTemplate.Review.PENDING)
    approve = request.POST.get("decision") == "approve"
    review_template(template, approve)
    messages.success(request, _("Template approved.") if approve else _("Template rejected."))
    return redirect(f"{reverse('composer_staff:idea_templates')}?tab=sent")


@_staff_required
@require_POST
def template_delete(request, template_id):
    get_object_or_404(IdeaTemplate, id=template_id).delete()
    messages.success(request, _("Template deleted."))
    return redirect("composer_staff:idea_templates")
