"""The operator's screen for the page banners' "See how it works" videos: one
YouTube link per page, seen by every brand.

Access is ``staff_required`` (``apps.accounts.decorators``).
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.decorators import staff_required

from . import help_videos
from .models import PageHelpVideo


@staff_required
@require_GET
def help_videos_list(request):
    saved = {row.page: row.video_url for row in PageHelpVideo.objects.all()}
    pages = [{"key": key, "label": label, "video_url": saved.get(key, "")} for key, label in PageHelpVideo.Page.choices]
    return render(request, "onboarding/staff_help_videos.html", {"admin_active": "help_videos", "pages": pages})


@staff_required
@require_POST
def help_video_save(request):
    page = request.POST.get("page", "")
    try:
        row = help_videos.save_video(page, request.POST.get("video_url", ""))
    except help_videos.InvalidVideoLinkError as exc:
        messages.error(request, str(exc))
    else:
        label = PageHelpVideo.Page(page).label
        if row:
            messages.success(request, _("Video saved for %(page)s.") % {"page": label})
        else:
            messages.success(request, _("Video removed from %(page)s.") % {"page": label})
    return redirect("onboarding_staff:help_videos")
