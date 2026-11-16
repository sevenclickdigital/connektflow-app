"""The operator's shelf of idea templates: the starting points every brand
sees in the create page's Templates tab.

Access is ``_staff_required``, borrowed from ``apps.billing.views`` like the
other screens of the administrative area.
"""

from django import forms
from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.billing.views import _staff_required

from .models import IdeaTemplate


class IdeaTemplateForm(forms.ModelForm):
    class Meta:
        model = IdeaTemplate
        fields = ["emoji", "title", "description", "body", "category", "is_active"]
        labels = {
            "emoji": gettext_lazy("Emoji"),
            "title": gettext_lazy("Title"),
            "description": gettext_lazy("Description"),
            "body": gettext_lazy("Text of the template"),
            "category": gettext_lazy("Category"),
            "is_active": gettext_lazy("Shown to customers"),
        }
        help_texts = {
            "description": gettext_lazy("One or two lines on the card, saying what the idea is."),
            "body": gettext_lazy(
                "What opens in the composer. Mark the parts to fill in with double braces, like {{topic}}."
            ),
        }
        widgets = {
            "emoji": forms.TextInput(attrs={"class": "form-input-styled w-20 text-center", "maxlength": 8}),
            "title": forms.TextInput(attrs={"class": "form-input-styled w-full"}),
            "description": forms.Textarea(attrs={"class": "form-input-styled w-full", "rows": 2}),
            "body": forms.Textarea(attrs={"class": "form-input-styled w-full", "rows": 10}),
            "category": forms.Select(attrs={"class": "form-input-styled w-full"}),
        }


@_staff_required
@require_GET
def template_list(request):
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    templates = IdeaTemplate.objects.all()
    if query:
        templates = templates.filter(Q(title__icontains=query) | Q(description__icontains=query))
    if category in IdeaTemplate.Category.values:
        templates = templates.filter(category=category)
    counts = dict(IdeaTemplate.objects.values_list("category").annotate(total=Count("id")))
    return render(
        request,
        "composer/staff_idea_templates.html",
        {
            "admin_active": "idea_templates",
            "templates": templates,
            "query": query,
            "category": category,
            "categories": [
                {"slug": slug, "label": label, "count": counts.get(slug, 0)}
                for slug, label in IdeaTemplate.Category.choices
            ],
            "total": IdeaTemplate.objects.count(),
            "hidden": IdeaTemplate.objects.filter(is_active=False).count(),
        },
    )


@_staff_required
@require_http_methods(["GET", "POST"])
def template_edit(request, template_id=None):
    """Create a template (no id) or change one."""
    template = get_object_or_404(IdeaTemplate, id=template_id) if template_id else None
    form = IdeaTemplateForm(request.POST or None, instance=template)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Template saved."))
        return redirect("composer_staff:idea_templates")
    return render(
        request,
        "composer/staff_idea_template_form.html",
        {"admin_active": "idea_templates", "form": form, "template": template},
    )


@_staff_required
@require_POST
def template_toggle(request, template_id):
    template = get_object_or_404(IdeaTemplate, id=template_id)
    template.is_active = not template.is_active
    template.save(update_fields=["is_active", "updated_at"])
    return redirect("composer_staff:idea_templates")


@_staff_required
@require_POST
def template_delete(request, template_id):
    get_object_or_404(IdeaTemplate, id=template_id).delete()
    messages.success(request, _("Template deleted."))
    return redirect("composer_staff:idea_templates")
