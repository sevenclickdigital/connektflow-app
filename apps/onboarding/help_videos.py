"""The "See how it works" videos of the page banners (PageHelpVideo).

The operator pastes any YouTube link (watch, youtu.be, shorts, live or
embed); what is stored is that link, and what the banner opens is the
privacy-enhanced embed of its video id, read from the link each time the
banner renders.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from django.utils.translation import gettext as _

from .models import PageHelpVideo

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_PATH_PREFIXES = ("shorts", "live", "embed", "v")


class InvalidVideoLinkError(ValueError):
    """The link is not a YouTube video the banner can play."""


def youtube_video_id(url: str) -> str | None:
    """The 11-character video id in a YouTube link, or None when the link is
    not one (another site, a channel, a playlist without a video)."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    candidate = ""
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        parts = [p for p in parsed.path.split("/") if p]
        if parts[:1] == ["watch"]:
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
            candidate = parts[1]
    return candidate if _VIDEO_ID.match(candidate) else None


def embed_url(video_id: str) -> str:
    """The player the modal frames: youtube-nocookie (no tracking cookies until
    play), starting on open, without suggestions from other channels at the end."""
    return f"https://www.youtube-nocookie.com/embed/{video_id}?autoplay=1&rel=0&modestbranding=1"


def video_for_page(page: str) -> dict | None:
    """What a banner needs to play its page's video, or None without one."""
    row = PageHelpVideo.objects.filter(page=page).only("video_url").first()
    if row is None:
        return None
    video_id = youtube_video_id(row.video_url)
    if not video_id:
        return None
    return {"embed_url": embed_url(video_id), "video_url": row.video_url}


def save_video(page: str, url: str) -> PageHelpVideo | None:
    """Set the page's video from the operator's link; an empty link removes it.

    Raises InvalidVideoLinkError (with a message for the operator) for an unknown
    page or a link that is not a YouTube video."""
    if page not in PageHelpVideo.Page.values:
        raise InvalidVideoLinkError(_("Unknown page."))
    url = (url or "").strip()
    if not url:
        PageHelpVideo.objects.filter(page=page).delete()
        return None
    if not youtube_video_id(url):
        raise InvalidVideoLinkError(_("Paste the link of a YouTube video, like https://www.youtube.com/watch?v=…"))
    row, _created = PageHelpVideo.objects.update_or_create(page=page, defaults={"video_url": url})
    return row
