"""The pieces a creative team can be asked to produce, with their exact sizes.

Grouped by network, the way design tools present them. A format either maps to
something this app publishes (``publish_format``, a ``PlatformPost.Format``
value, on the accounts of ``platforms``) or is an art-only piece — a page cover,
an ad — that is delivered as a file and never becomes a post.

Sizes are the networks' recommended upload sizes; what matters for checking a
delivered file is the aspect ratio, which is why ``ratio_matches`` compares
proportions rather than pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd

from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

IMAGE = "image"
VIDEO = "video"
ANY = "any"

# How far a delivered file's width/height may drift from the format's before
# it is reported, as a fraction of the expected ratio (1080×1350 vs 1080×1349).
RATIO_TOLERANCE = 0.02


@dataclass(frozen=True)
class Network:
    key: str
    label: str
    platforms: tuple[str, ...]


@dataclass(frozen=True)
class CreativeFormat:
    key: str
    network: str
    label: str
    width: int
    height: int
    media: str = IMAGE
    publish_format: str | None = "default"

    @property
    def ratio_label(self) -> str:
        divisor = gcd(self.width, self.height)
        w, h = self.width // divisor, self.height // divisor
        named = {(1, 1): "1:1", (4, 5): "4:5", (9, 16): "9:16", (16, 9): "16:9", (2, 3): "2:3", (4, 3): "4:3"}
        if (w, h) in named:
            return named[(w, h)]
        return f"{self.width / self.height:.2f}:1"

    @property
    def is_publishable(self) -> bool:
        return self.publish_format is not None


NETWORKS: tuple[Network, ...] = (
    Network("instagram", "Instagram", ("instagram", "instagram_login")),
    Network("facebook", "Facebook", ("facebook",)),
    Network("linkedin", "LinkedIn", ("linkedin_personal", "linkedin_company")),
    Network("tiktok", "TikTok", ("tiktok",)),
    Network("youtube", "YouTube", ("youtube",)),
    Network("pinterest", "Pinterest", ("pinterest",)),
    Network("google_business", "Google Business", ("google_business",)),
    Network("x", "X", ("x",)),
    Network("threads", "Threads", ("threads",)),
    Network("bluesky", "Bluesky", ("bluesky",)),
    Network("mastodon", "Mastodon", ("mastodon",)),
    Network("devto", "DEV.to", ("devto",)),
)

FORMATS: tuple[CreativeFormat, ...] = (
    # Instagram
    CreativeFormat("ig_feed_portrait", "instagram", _("Portrait post"), 1080, 1350),
    CreativeFormat("ig_feed_square", "instagram", _("Square post"), 1080, 1080),
    CreativeFormat("ig_story", "instagram", pgettext_lazy("publishing format", "Story"), 1080, 1920, ANY, "story"),
    CreativeFormat("ig_reel", "instagram", _("Reels"), 1080, 1920, VIDEO, "reel"),
    CreativeFormat("ig_profile", "instagram", _("Profile photo"), 320, 320, IMAGE, None),
    # Facebook
    CreativeFormat("fb_post_square", "facebook", _("Square post"), 1080, 1080),
    CreativeFormat("fb_post_landscape", "facebook", _("Landscape post"), 1200, 630),
    CreativeFormat("fb_story", "facebook", pgettext_lazy("publishing format", "Story"), 1080, 1920, ANY, "story"),
    CreativeFormat("fb_reel", "facebook", _("Reels"), 1080, 1920, VIDEO, "reel"),
    CreativeFormat("fb_cover", "facebook", _("Page cover"), 1640, 624, IMAGE, None),
    CreativeFormat("fb_event_cover", "facebook", _("Event cover"), 1920, 1005, IMAGE, None),
    CreativeFormat("fb_ad", "facebook", _("Ad"), 1080, 1080, IMAGE, None),
    CreativeFormat("fb_story_ad", "facebook", _("Story ad"), 1080, 1920, ANY, None),
    CreativeFormat("fb_profile", "facebook", _("Profile photo"), 720, 720, IMAGE, None),
    # LinkedIn
    CreativeFormat("li_post_square", "linkedin", _("Square post"), 1200, 1200),
    CreativeFormat("li_post_landscape", "linkedin", _("Landscape post"), 1200, 627),
    CreativeFormat("li_cover", "linkedin", _("Page cover"), 1128, 191, IMAGE, None),
    # TikTok
    CreativeFormat("tt_video", "tiktok", _("Video"), 1080, 1920, VIDEO),
    # YouTube
    CreativeFormat("yt_video", "youtube", _("Video"), 1920, 1080, VIDEO),
    CreativeFormat("yt_short", "youtube", _("Short"), 1080, 1920, VIDEO),
    CreativeFormat("yt_thumbnail", "youtube", _("Thumbnail"), 1280, 720, IMAGE, None),
    CreativeFormat("yt_banner", "youtube", _("Channel banner"), 2560, 1440, IMAGE, None),
    # Pinterest
    CreativeFormat("pin_standard", "pinterest", _("Pin"), 1000, 1500),
    # Google Business
    CreativeFormat("gbp_photo", "google_business", _("Photo"), 1200, 900),
    CreativeFormat("gbp_profile", "google_business", _("Profile photo"), 720, 720, IMAGE, "profile_photo"),
    # X
    CreativeFormat("x_post", "x", _("Post image"), 1600, 900),
    # Threads
    CreativeFormat("threads_post", "threads", _("Post"), 1080, 1350),
    # Bluesky
    CreativeFormat("bsky_post", "bluesky", _("Post image"), 1200, 675),
    # Mastodon
    CreativeFormat("masto_post", "mastodon", _("Post image"), 1200, 675),
    # DEV.to
    CreativeFormat("devto_cover", "devto", _("Article cover"), 1000, 420, IMAGE, None),
)

_BY_KEY = {f.key: f for f in FORMATS}
_NETWORKS_BY_KEY = {n.key: n for n in NETWORKS}


def get(key: str) -> CreativeFormat | None:
    return _BY_KEY.get(key)


def network(key: str) -> Network:
    return _NETWORKS_BY_KEY[key]


def catalogue(accounts=(), visible_platforms=None) -> list[dict]:
    """Networks with their formats and the workspace's accounts on each.

    Only networks with a platform in ``visible_platforms`` (what the connect
    page offers; every one when None) are listed. Networks with a connected
    account come first, in catalogue order.
    """
    groups = []
    for net in NETWORKS:
        if visible_platforms is not None and not set(net.platforms) & set(visible_platforms):
            continue
        groups.append(
            {
                "network": net,
                "accounts": [a for a in accounts if a.platform in net.platforms],
                "formats": [f for f in FORMATS if f.network == net.key],
            }
        )
    return sorted(groups, key=lambda g: not g["accounts"])


def ratio_matches(fmt_width: int, fmt_height: int, width: int, height: int) -> bool:
    if not (width and height):
        return True
    expected = fmt_width / fmt_height
    return abs(width / height - expected) <= expected * RATIO_TOLERANCE
