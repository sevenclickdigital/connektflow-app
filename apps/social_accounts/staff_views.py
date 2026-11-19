"""The operator's screen for which networks customers may connect.

One switch per platform over ``PlatformVisibility``: a hidden platform leaves
the connect page, the sidebar's connect shortcuts and the composer's "Connect
channel" card for every brand. Meant for turning a network on once its app is
configured. Access is ``_staff_required``, like the other screens of the
administrative area.
"""

from django.http import HttpResponseBadRequest
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.billing.views import _staff_required
from apps.common.htmx import toast_response

from .catalog import platform_category, platform_description
from .models import PlatformCredential, PlatformVisibility
from .views import _get_configured_platforms


@_staff_required
@require_GET
def platforms_list(request):
    hidden = set(PlatformVisibility.objects.filter(is_visible=False).values_list("platform", flat=True))
    configured = _get_configured_platforms(None)
    platforms = [
        {
            "value": value,
            "label": label,
            "description": platform_description(value),
            "category": platform_category(value),
            "visible": value not in hidden,
            "configured": value in configured,
        }
        for value, label in PlatformCredential.Platform.choices
    ]
    return render(
        request,
        "social_accounts/staff_platforms.html",
        {"admin_active": "platforms", "platforms": platforms},
    )


@_staff_required
@require_POST
def platform_toggle(request):
    platform = request.POST.get("platform", "")
    visible = request.POST.get("visible") == "on"
    try:
        row = PlatformVisibility.set_visible(platform, visible)
    except ValueError:
        return HttpResponseBadRequest(_("Unknown platform."))
    label = row.get_platform_display()
    if visible:
        return toast_response(tone="success", title=_("%(platform)s is shown to customers.") % {"platform": label})
    return toast_response(tone="info", title=_("%(platform)s is hidden from customers.") % {"platform": label})
