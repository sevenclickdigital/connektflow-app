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
        named = {
            (1, 1): "1:1",
            (4, 5): "4:5",
            (9, 16): "9:16",
            (16, 9): "16:9",
            (2, 3): "2:3",
            (4, 3): "4:3",
            (3, 4): "3:4",
            (2, 1): "2:1",
            (3, 1): "3:1",
        }
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
    Network("whatsapp", "WhatsApp", ("whatsapp",)),
    # Channels the app does not connect to: their pieces are made here and posted by hand.
    Network("shopee", "Shopee", ()),
    Network("shopee_video", "Shopee Vídeo", ()),
    Network("kwai", "Kwai", ()),
)

FORMATS: tuple[CreativeFormat, ...] = (
    # Instagram
    CreativeFormat("ig_feed_portrait", "instagram", _("Portrait post"), 1080, 1350),
    CreativeFormat("ig_feed_square", "instagram", _("Square post"), 1080, 1080),
    CreativeFormat("ig_story", "instagram", pgettext_lazy("publishing format", "Story"), 1080, 1920, ANY, "story"),
    CreativeFormat("ig_reel", "instagram", _("Reels"), 1080, 1920, VIDEO, "reel"),
    CreativeFormat("ig_profile", "instagram", _("Profile photo"), 320, 320, IMAGE, None),
    CreativeFormat("ig_feed_tall", "instagram", _("Post 3:4"), 1080, 1440),
    CreativeFormat("ig_ad_portrait", "instagram", _("Ad 4:5"), 1080, 1350, IMAGE, None),
    CreativeFormat("ig_ad_feed", "instagram", _("Feed ad"), 1080, 1080, IMAGE, None),
    CreativeFormat("ig_story_ad", "instagram", _("Story ad"), 1080, 1920, ANY, None),
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
    CreativeFormat("fb_post_portrait", "facebook", _("Portrait post"), 1080, 1350),
    CreativeFormat("fb_video", "facebook", _("Video"), 1280, 720, VIDEO),
    CreativeFormat("fb_ad_landscape", "facebook", _("Landscape ad"), 1200, 628, IMAGE, None),
    # LinkedIn
    CreativeFormat("li_post_square", "linkedin", _("Square post"), 1200, 1200),
    CreativeFormat("li_post_landscape", "linkedin", _("Landscape post"), 1200, 627),
    CreativeFormat("li_cover", "linkedin", _("Page cover"), 1128, 191, IMAGE, None),
    CreativeFormat("li_video", "linkedin", _("Video"), 1920, 1080, VIDEO),
    CreativeFormat("li_carousel", "linkedin", _("Carousel"), 1080, 1350),
    CreativeFormat("li_background", "linkedin", _("Background photo"), 1584, 396, IMAGE, None),
    CreativeFormat("li_article_cover", "linkedin", _("Article cover"), 1920, 1080, IMAGE, None),
    CreativeFormat("li_ad_image", "linkedin", _("Single image ad"), 1200, 627, IMAGE, None),
    CreativeFormat("li_ad_video", "linkedin", _("Video ad"), 1920, 1080, VIDEO, None),
    CreativeFormat("li_profile", "linkedin", _("Profile photo"), 400, 400, IMAGE, None),
    # TikTok
    CreativeFormat("tt_video", "tiktok", _("Video"), 1080, 1920, VIDEO),
    CreativeFormat("tt_photo_tall", "tiktok", _("Photo mode 9:16"), 1080, 1920),
    CreativeFormat("tt_photo_portrait", "tiktok", _("Photo mode 3:4"), 1080, 1440),
    CreativeFormat("tt_photo_square", "tiktok", _("Photo mode square"), 1080, 1080),
    CreativeFormat("tt_story", "tiktok", pgettext_lazy("publishing format", "Story"), 1080, 1920, ANY, None),
    CreativeFormat("tt_profile", "tiktok", _("Profile photo"), 200, 200, IMAGE, None),
    # YouTube
    CreativeFormat("yt_video", "youtube", _("Video"), 1920, 1080, VIDEO),
    CreativeFormat("yt_short", "youtube", _("Short"), 1080, 1920, VIDEO),
    CreativeFormat("yt_thumbnail", "youtube", _("Thumbnail"), 1280, 720, IMAGE, None),
    CreativeFormat("yt_banner", "youtube", _("Channel banner"), 2560, 1440, IMAGE, None),
    CreativeFormat("yt_logo", "youtube", _("Channel logo"), 800, 800, IMAGE, None),
    CreativeFormat("yt_profile", "youtube", _("Profile photo"), 800, 800, IMAGE, None),
    CreativeFormat("yt_chapter", "youtube", _("Video chapter"), 1280, 720, IMAGE, None),
    CreativeFormat("yt_live", "youtube", _("Live stream video"), 1920, 1080, VIDEO, None),
    CreativeFormat("yt_display_ad", "youtube", _("Display ad"), 300, 250, IMAGE, None),
    CreativeFormat("yt_ad_landscape", "youtube", _("Video ad (landscape)"), 1920, 1080, VIDEO, None),
    CreativeFormat("yt_ad_portrait", "youtube", _("Video ad (portrait)"), 1080, 1920, VIDEO, None),
    # Pinterest
    CreativeFormat("pin_standard", "pinterest", _("Pin"), 1000, 1500),
    CreativeFormat("pin_tall", "pinterest", _("Pin 9:16"), 1080, 1920),
    CreativeFormat("pin_ad_static", "pinterest", _("Static ad"), 1000, 1500, IMAGE, None),
    CreativeFormat("pin_ad_carousel", "pinterest", _("Carousel ad"), 1000, 1500, IMAGE, None),
    CreativeFormat("pin_ad_video", "pinterest", _("Square video ad"), 1000, 1000, VIDEO, None),
    # Google Business
    CreativeFormat("gbp_photo", "google_business", _("Photo"), 1200, 900),
    CreativeFormat("gbp_profile", "google_business", _("Profile photo"), 720, 720, IMAGE, "profile_photo"),
    # X
    CreativeFormat("x_post", "x", _("Post image"), 1600, 900),
    CreativeFormat("x_video", "x", _("Video"), 1920, 1080, VIDEO),
    CreativeFormat("x_header", "x", _("Header"), 1500, 500, IMAGE, None),
    CreativeFormat("x_ad", "x", _("Ad"), 1600, 900, IMAGE, None),
    CreativeFormat("x_profile", "x", _("Profile photo"), 400, 400, IMAGE, None),
    # Threads
    CreativeFormat("threads_post", "threads", _("Post"), 1080, 1350),
    # Bluesky
    CreativeFormat("bsky_post", "bluesky", _("Post image"), 1200, 675),
    # Mastodon
    CreativeFormat("masto_post", "mastodon", _("Post image"), 1200, 675),
    # DEV.to
    CreativeFormat("devto_cover", "devto", _("Article cover"), 1000, 420, IMAGE, None),
    # WhatsApp: a messaging channel here, so its pieces are delivered as files, never posted.
    CreativeFormat("wa_status", "whatsapp", _("Status"), 1080, 1920, ANY, None),
    CreativeFormat("wa_post_portrait", "whatsapp", _("Portrait post"), 1080, 1350, IMAGE, None),
    CreativeFormat("wa_post_landscape", "whatsapp", _("Landscape post"), 1280, 720, IMAGE, None),
    CreativeFormat("wa_post_square", "whatsapp", _("Square post"), 1080, 1080, IMAGE, None),
    CreativeFormat("wa_card", "whatsapp", _("Vertical card"), 1080, 1920, IMAGE, None),
    # Shopee
    CreativeFormat("shopee_product", "shopee", _("Product photo"), 1024, 1024, IMAGE, None),
    CreativeFormat("shopee_product_video", "shopee", _("Product video"), 1080, 1080, VIDEO, None),
    CreativeFormat("shopee_banner", "shopee", _("Shop banner"), 1200, 600, IMAGE, None),
    # Shopee Vídeo
    CreativeFormat("shopee_video", "shopee_video", _("Video"), 1080, 1920, VIDEO, None),
    CreativeFormat("shopee_video_cover", "shopee_video", _("Video cover"), 1080, 1920, IMAGE, None),
    # Kwai
    CreativeFormat("kwai_video", "kwai", _("Video"), 1080, 1920, VIDEO, None),
    CreativeFormat("kwai_cover", "kwai", _("Video cover"), 1080, 1920, IMAGE, None),
    CreativeFormat("kwai_profile", "kwai", _("Profile photo"), 200, 200, IMAGE, None),
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
    page offers; every one when None) are listed, and the ones the app does not
    connect to (no platforms: Shopee, Kwai), always. Networks with a connected
    account come first, in catalogue order.
    """
    groups = []
    for net in NETWORKS:
        if net.platforms and visible_platforms is not None and not set(net.platforms) & set(visible_platforms):
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
    """Whether ``width`` × ``height`` has the proportions of ``fmt_width`` × ``fmt_height``;
    true when either size is unknown (an item without a size takes any file)."""
    if not (width and height and fmt_width and fmt_height):
        return True
    expected = fmt_width / fmt_height
    return abs(width / height - expected) <= expected * RATIO_TOLERANCE


# What a content template or an idea says the creative team will produce
# ("art items"): ``[{"format": <key>, "count": n, "note": "…"}]``. The format gives
# the piece's kind and size; ``count`` how many (a carousel's slides); ``note`` what
# each one shows. Carried to the task's "Request arts" (tasks.services.request_arts).
MAX_ART_ITEMS = 20
MAX_ART_COUNT = 20
MAX_ART_NOTE = 200


def clean_art_items(raw) -> list[dict]:
    """``raw`` (a list of dicts, as posted) kept to known formats, each once, with a count
    between 1 and MAX_ART_COUNT and a note of at most MAX_ART_NOTE characters."""
    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("format", ""))
        if key in seen or get(key) is None:
            continue
        try:
            count = int(entry.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        seen.add(key)
        items.append(
            {
                "format": key,
                "count": max(1, min(MAX_ART_COUNT, count)),
                "note": str(entry.get("note", "")).strip()[:MAX_ART_NOTE],
            }
        )
        if len(items) >= MAX_ART_ITEMS:
            break
    return items


def art_items_payload(items) -> list[dict]:
    """The art items as screens show them: network (its name, and its key for the icon), the
    format's name, size, publishing format (``default``, ``story``, ``reel``…; "" for an
    art-only piece), count, note."""
    rows = []
    for item in clean_art_items(items):
        fmt = _BY_KEY[item["format"]]
        rows.append(
            {
                **item,
                "network": str(network(fmt.network).label),
                "network_key": fmt.network,
                "label": str(fmt.label),
                "size": f"{fmt.width} × {fmt.height}",
                "publish_format": fmt.publish_format or "",
            }
        )
    return rows


def format_options() -> list[dict]:
    """Every network with its formats, for a format picker: ``{"label", "formats": [{"key", "label", "size"}]}``."""
    return [
        {
            "label": net.label,
            "formats": [
                {"key": f.key, "label": str(f.label), "size": f"{f.width} × {f.height}"}
                for f in FORMATS
                if f.network == net.key
            ],
        }
        for net in NETWORKS
    ]
