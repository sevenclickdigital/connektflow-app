"""The operator's shelf of special dates: segments and the dates in each.

Global dates (no segment) reach every brand; a segment's dates reach the
brands that chose it. The built-in holidays live in ``holidays.py`` and are
listed here read-only, so the operator sees what is already covered.

Access is ``staff_required`` (``apps.accounts.decorators``).
"""

import json
from datetime import date

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import staff_required
from apps.common.validators import clean_display_name
from apps.media_library.validators import image_storage_filename
from apps.tasks import creative_formats

from .holidays import special_dates_between
from .models import DateSegment, SpecialDate

#: Emoji are a few code points at most; anything longer is not one.
_EMOJI_MAX = 8

#: What a date's picture may be: it is shown in the customer's panel.
_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")
_IMAGE_MAX_BYTES = 2 * 1024 * 1024


def _emoji(request):
    return request.POST.get("emoji", "").strip()[:_EMOJI_MAX]


def _back():
    return redirect("calendar_staff:dates")


@staff_required
@require_GET
def dates(request):
    today = timezone.localdate()
    built_in = special_dates_between(date(today.year, 1, 1), date(today.year, 12, 31))
    segments = (
        DateSegment.objects.annotate(brand_count=Count("workspace_choices", distinct=True))
        .prefetch_related("dates")
        .order_by("name")
    )
    return render(
        request,
        "calendar/staff_dates.html",
        {
            "admin_active": "special_dates",
            "segments": segments,
            "global_dates": SpecialDate.objects.filter(segment__isnull=True),
            # The ones from the code; the operator's global dates are the
            # editable list beside them.
            "built_in_dates": [d for d in built_in if d["built_in"]],
            "year": today.year,
            "kind_choices": SpecialDate.Kind.choices,
            "art_format_options": creative_formats.format_options(),
        },
    )


@staff_required
@require_POST
def segment_create(request):
    name = clean_display_name(request.POST.get("name", ""))[:100]
    if not name:
        messages.error(request, _("Enter the name of the segment."))
    elif DateSegment.objects.filter(name__iexact=name).exists():
        messages.error(request, _("There is already a segment with this name."))
    else:
        DateSegment.objects.create(name=name, emoji=_emoji(request))
        messages.success(request, _("Segment created."))
    return _back()


@staff_required
@require_POST
def segment_update(request, segment_id):
    """Rename a segment, change its emoji, or take it off / put it back on offer."""
    segment = get_object_or_404(DateSegment, id=segment_id)
    if "is_active" in request.POST:
        segment.is_active = request.POST["is_active"] == "1"
        segment.save(update_fields=["is_active"])
        return _back()
    name = clean_display_name(request.POST.get("name", ""))[:100]
    if not name:
        messages.error(request, _("Enter the name of the segment."))
    elif DateSegment.objects.filter(name__iexact=name).exclude(id=segment.id).exists():
        messages.error(request, _("There is already a segment with this name."))
    else:
        segment.name = name
        segment.emoji = _emoji(request)
        segment.save(update_fields=["name", "emoji"])
        messages.success(request, _("Segment updated."))
    return _back()


@staff_required
@require_POST
def segment_delete(request, segment_id):
    get_object_or_404(DateSegment, id=segment_id).delete()
    messages.success(request, _("Segment deleted, with its dates."))
    return _back()


def _posted_art_items(request) -> list:
    """The add-date form's "art_items" (composer/partials/_template_art_items.html), as posted."""
    try:
        raw = json.loads(request.POST.get("art_items") or "[]")
    except ValueError:
        return []
    return raw if isinstance(raw, list) else []


@staff_required
@require_POST
def date_create(request):
    """Add a date: global when no segment is sent, else of that segment."""
    segment = None
    if request.POST.get("segment"):
        segment = get_object_or_404(DateSegment, id=request.POST["segment"])
    name = clean_display_name(request.POST.get("name", ""))[:120]
    try:
        month, day = int(request.POST.get("month", "")), int(request.POST.get("day", ""))
        # A leap year, so 29 February is accepted (it is skipped in the others).
        date(2024, month, day)
    except ValueError:
        messages.error(request, _("Enter a valid day and month."))
        return _back()
    if not name:
        messages.error(request, _("Enter the name of the date."))
        return _back()
    image = request.FILES.get("image")
    if image:
        storage_name = image_storage_filename(image, _IMAGE_TYPES)
        if not storage_name or image.size > _IMAGE_MAX_BYTES:
            messages.error(request, _("The image must be a JPEG, PNG or WebP of up to 2 MB."))
            return _back()
        image.name = storage_name
    kind = request.POST.get("kind", "")
    SpecialDate.objects.create(
        segment=segment,
        name=name,
        emoji=_emoji(request),
        month=month,
        day=day,
        kind=kind if kind in SpecialDate.Kind.values else SpecialDate.Kind.COMMERCIAL,
        description=request.POST.get("description", "").strip()[:300],
        suggestions=request.POST.get("suggestions", "").strip()[:2000],
        tags=request.POST.get("tags", "").strip()[:200],
        image=image or "",
        briefing=request.POST.get("briefing", "").strip()[:2000],
        art_items=creative_formats.clean_art_items(_posted_art_items(request)),
    )
    messages.success(request, _("Date added."))
    return _back()


@staff_required
@require_POST
def date_delete(request, date_id):
    special_date = get_object_or_404(SpecialDate, id=date_id)
    if special_date.image:
        special_date.image.delete(save=False)
    special_date.delete()
    messages.success(request, _("Date removed."))
    return _back()
