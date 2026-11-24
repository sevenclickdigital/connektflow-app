"""The operator's screens for the pages' tutorials: one YouTube link per page,
opened by the page banner's "See how it works" and shown in the help centre,
and the tutorial's text under it there.

Access is ``staff_required`` (``apps.accounts.decorators``).
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.decorators import staff_required

from . import help_videos
from .models import PageHelpVideo


@staff_required
@require_GET
def help_videos_list(request):
    saved = {row.page: row for row in PageHelpVideo.objects.all()}
    pages = [
        {
            "key": key,
            "label": label,
            "video_url": saved[key].video_url if key in saved else "",
            "has_text": bool(key in saved and saved[key].tutorial_markdown),
        }
        for key, label in PageHelpVideo.Page.choices
    ]
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


@staff_required
@require_http_methods(["GET", "POST"])
def tutorial_edit(request, page):
    """One page's tutorial: its video link and the text the help centre shows under it."""
    if page not in PageHelpVideo.Page.values:
        raise Http404("Unknown page.")
    row = PageHelpVideo.objects.filter(page=page).first()
    video_url = row.video_url if row else ""
    text = row.tutorial_markdown if row else ""
    if request.method == "POST":
        video_url = request.POST.get("video_url", "")
        text = request.POST.get("tutorial_markdown", "")
        try:
            help_videos.save_tutorial(page, video_url, text)
        except help_videos.InvalidVideoLinkError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, _("Tutorial saved for %(page)s.") % {"page": PageHelpVideo.Page(page).label})
            return redirect("onboarding_staff:tutorial_edit", page=page)
    return render(
        request,
        "onboarding/staff_tutorial_edit.html",
        {
            "admin_active": "help_videos",
            "page_key": page,
            "page_label": PageHelpVideo.Page(page).label,
            "video_url": video_url,
            "text": text,
        },
    )
