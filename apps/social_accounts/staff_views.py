"""The operator's screen for which networks and opportunity sources customers may connect.

One switch per platform over ``PlatformVisibility``: a hidden platform leaves
the connect page, the sidebar's connect shortcuts and the composer's "Connect
channel" card for every brand; a hidden opportunity source (``apps.radar.sources``)
leaves the Platforms page and the Radar for brands that have not linked it.
Meant for turning one on once it is configured. Access is ``superadmin_required``,
like the other screens of the administrative area.
"""

from django.http import HttpResponseBadRequest
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import superadmin_required
from apps.common.htmx import toast_response
from apps.radar.sources import UPCOMING, all_sources

from .catalog import platform_category, platform_description
from .models import PlatformCredential, PlatformVisibility
from .views import _get_configured_platforms


@superadmin_required
@require_GET
def platforms_list(request):
    hidden = PlatformVisibility.hidden_keys()
    configured = _get_configured_platforms(None)
    upcoming = {source.key for source in UPCOMING}
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
    sources = [
        {
            "value": source.key,
            "label": source.label,
            "description": source.description,
            "visible": source.key not in hidden,
            "upcoming": source.key in upcoming,
        }
        for source in all_sources()
    ]
    return render(
        request,
        "social_accounts/staff_platforms.html",
        {"admin_active": "platforms", "platforms": platforms, "sources": sources},
    )


@superadmin_required
@require_POST
def platform_toggle(request):
    platform = request.POST.get("platform", "")
    visible = request.POST.get("visible") == "on"
    try:
        PlatformVisibility.set_visible(platform, visible)
    except ValueError:
        return HttpResponseBadRequest(_("Unknown platform."))
    labels = dict(PlatformCredential.Platform.choices) | {s.key: s.label for s in all_sources()}
    label = labels[platform]
    if visible:
        return toast_response(tone="success", title=_("%(platform)s is shown to customers.") % {"platform": label})
    return toast_response(tone="info", title=_("%(platform)s is hidden from customers.") % {"platform": label})
