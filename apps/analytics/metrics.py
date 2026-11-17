"""Platform-aware metric catalog and engagement-rate derivation rules.

This is the single source of truth for which metrics each platform exposes
and how to label/format them in the UI. The shape of ``PLATFORM_METRICS``
mirrors the design's ``analytics/data.js``.
"""

from __future__ import annotations

from django.utils.translation import gettext_noop

# Kinds drive formatting (count vs percent vs minutes) in templates.
# Labels stay in English here — the agent API returns them as they are — and are
# only marked for the catalogue: templates translate them with {% translate label %}.
METRICS: dict[str, dict[str, str]] = {
    "impressions": {"label": gettext_noop("Impressions"), "kind": "count"},
    "reach": {"label": gettext_noop("Reach"), "kind": "count"},
    "views": {"label": gettext_noop("Views"), "kind": "count"},
    "plays": {"label": gettext_noop("Plays"), "kind": "count"},
    "likes": {"label": gettext_noop("Likes"), "kind": "count"},
    "reactions": {"label": gettext_noop("Reactions"), "kind": "count"},
    "comments": {"label": gettext_noop("Comments"), "kind": "count"},
    "replies": {"label": gettext_noop("Replies"), "kind": "count"},
    "shares": {"label": gettext_noop("Shares"), "kind": "count"},
    "reposts": {"label": gettext_noop("Reposts"), "kind": "count"},
    "saves": {"label": gettext_noop("Saves"), "kind": "count"},
    "clicks": {"label": gettext_noop("Link clicks"), "kind": "count"},
    "outbound": {"label": gettext_noop("Outbound clicks"), "kind": "count"},
    "follows": {"label": gettext_noop("New follows"), "kind": "count"},
    "followers": {"label": gettext_noop("Followers"), "kind": "count"},
    "subscribers": {"label": gettext_noop("Subscribers"), "kind": "count"},
    "watch_time": {"label": gettext_noop("Watch time"), "kind": "minutes"},
    "avg_view_pct": {"label": gettext_noop("Avg view %"), "kind": "percent"},
    "engagement": {"label": gettext_noop("Engagement rate"), "kind": "percent"},
}

# Metrics that exist for the account/channel but never per individual post
# (you can't attribute new followers to one post via the platform APIs).
# These appear in account summaries but not in the per-post table.
ACCOUNT_ONLY: set[str] = {"follows", "followers", "subscribers"}

# Which metrics each platform's API reports (after the scope upgrades in the
# plan are in place). Verified against each platform's published insights API.
PLATFORM_METRICS: dict[str, list[str]] = {
    # IG media insights: reach, views (replaced impressions Apr-2025), likes,
    # comments, saved, shares, total_interactions. ``followers`` (total) is
    # account-only: Meta deprecated the IG ``follower_count`` insight, so we
    # snapshot the profile follower total and derive growth from day-over-day
    # deltas (same pattern as TikTok) rather than a per-day ``follows`` metric.
    "instagram": ["reach", "views", "likes", "comments", "saves", "shares", "followers", "engagement"],
    "instagram_login": ["reach", "views", "likes", "comments", "saves", "shares", "followers", "engagement"],
    # FB post insights: media views, unique media views, reactions, comments, shares, clicks.
    "facebook": ["views", "reach", "reactions", "comments", "shares", "clicks", "follows", "engagement"],
    # LinkedIn share statistics: impressions, reactions, comments, reposts, clicks, engagement.
    "linkedin_company": ["impressions", "reactions", "comments", "reposts", "clicks", "follows", "engagement"],
    # LinkedIn Personal: only socialActions counts (no impressions/reach per API).
    "linkedin_personal": ["likes", "comments", "shares"],
    # YouTube Analytics: views, watch_time, avg_view_pct, likes, comments, shares, subscribers gained.
    "youtube": ["views", "watch_time", "avg_view_pct", "likes", "comments", "shares", "subscribers"],
    # TikTok video metrics: view/like/comment/share counts from /v2/video/query/.
    # Account-level followers require ``user.info.stats``, which we don't request.
    # watch_time is intentionally absent: TikTok has no public per-video
    # Analytics-style endpoint for it yet.
    "tiktok": ["views", "likes", "comments", "shares", "engagement"],
    # Bluesky / AT Protocol post aggregates: like, repost, reply counts (no impressions/views).
    "bluesky": ["likes", "reposts", "replies", "follows"],
    # Threads insights: views, likes, replies, reposts; account follower growth.
    "threads": ["views", "likes", "replies", "reposts", "follows"],
    # Pinterest pin analytics: impressions, saves, pin clicks, outbound clicks, engagement.
    "pinterest": ["impressions", "saves", "clicks", "outbound", "engagement"],
    # Google Business Profile performance: search/map impressions, clicks, calls, directions.
    "google_business": ["impressions", "clicks"],
    # Mastodon: only favourites/reblogs/replies from the public status record.
    "mastodon": ["likes", "reposts", "replies"],
    # X public metrics: impressions, likes, replies, reposts (+quotes), bookmarks.
    "x": ["impressions", "likes", "replies", "reposts", "saves", "engagement"],
}

# The "hero" metric each platform defaults the big chart and table sort to.
PLATFORM_PRIMARY: dict[str, str] = {
    "instagram": "reach",
    "instagram_login": "reach",
    "facebook": "reach",
    "linkedin_company": "impressions",
    "linkedin_personal": "likes",
    "youtube": "views",
    "tiktok": "views",
    "bluesky": "likes",
    "threads": "views",
    "pinterest": "impressions",
    "google_business": "impressions",
    "mastodon": "likes",
    "x": "impressions",
}

# Brand-orange override for charts; per-platform colors used as a tweak.
PLATFORM_COLOR: dict[str, str] = {
    "instagram": "#E4405F",
    "instagram_login": "#E4405F",
    "facebook": "#1877F2",
    "linkedin_personal": "#0A66C2",
    "linkedin_company": "#0A66C2",
    "youtube": "#FF0000",
    "tiktok": "#111111",
    "bluesky": "#0085FF",
    "threads": "#111111",
    "pinterest": "#BD081C",
    "google_business": "#4285F4",
    "mastodon": "#6364FF",
    "x": "#111111",
}

# Metrics that contribute to engagement rate (numerator).
ENGAGEMENT_PARTS: list[str] = [
    "likes",
    "reactions",
    "comments",
    "replies",
    "shares",
    "reposts",
    "saves",
    "clicks",
    "outbound",
]

# Candidate denominators in priority order (first match wins). If none of
# these exist on the platform, the engagement card is suppressed in the UI.
ENGAGEMENT_DENOMINATORS: list[str] = ["views", "reach", "impressions", "plays"]


def post_metrics_for(platform: str) -> list[str]:
    """Metrics that show up in per-post views (table columns, detail drawer).

    Account-only metrics (follows, subscribers) are filtered out — you can't
    attribute them to a single post.
    """
    return [m for m in PLATFORM_METRICS.get(platform, []) if m not in ACCOUNT_ONLY]


def hero_card_metrics(platform: str) -> list[str]:
    """Top-line "reach-like" metrics that get their own StatCard in the hero row.

    Engagement components and the rate itself live in the EngagementCard, and
    account-only growth metrics live in the header — so neither shows up here.
    """
    if platform == "youtube":
        return [m for m in PLATFORM_METRICS[platform] if m not in ACCOUNT_ONLY]
    return [
        m
        for m in PLATFORM_METRICS.get(platform, [])
        if m not in ENGAGEMENT_PARTS and m != "engagement" and m not in ACCOUNT_ONLY
    ]


def has_engagement_card(platform: str) -> bool:
    """True if the platform has both engagement parts AND a reach-like denom.

    Bluesky/Mastodon/LinkedIn-Personal have no reach denom, so we render
    only count cards for them (per the plan's "hard platform gap" handling).
    """
    if platform == "youtube":
        return False
    metrics = PLATFORM_METRICS.get(platform, [])
    has_parts = any(p in metrics for p in ENGAGEMENT_PARTS)
    has_denom = any(d in metrics for d in ENGAGEMENT_DENOMINATORS)
    return has_parts and has_denom
