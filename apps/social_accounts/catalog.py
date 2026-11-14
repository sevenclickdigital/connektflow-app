"""How each platform is presented on the Social Accounts page.

One short description and one category per platform, used by the account
cards and the "connect other platforms" grid. A platform missing here still
shows, under "Other" and without a description, so adding a provider never
hides it from the page.
"""

from django.utils.translation import gettext_lazy as _

# Order is the order of the category chips.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("social", _("Social networks")),
    ("messaging", _("Messaging")),
    ("video", _("Videos")),
    ("professional", _("Professional")),
    ("discovery", _("Discovery")),
    ("local", _("Local business")),
    ("other", _("Other")),
)

_PLATFORMS = {
    "facebook": (_("Your Pages & Groups"), "social"),
    "instagram": (_("For accounts linked to a Facebook Page"), "social"),
    "instagram_login": (_("Sign in with Instagram · no Facebook needed"), "social"),
    "threads": (_("Text & Media"), "social"),
    "x": (_("Posts, photos & video"), "social"),
    "bluesky": (_("AT Protocol"), "social"),
    "mastodon": (_("Federated social"), "social"),
    "youtube": (_("Videos & Shorts"), "video"),
    "tiktok": (_("Video content"), "video"),
    "linkedin_personal": (_("Personal profile"), "professional"),
    "linkedin_company": (_("Company pages"), "professional"),
    "devto": (_("Articles & posts"), "professional"),
    "pinterest": (_("Pins & Boards"), "discovery"),
    "google_business": (_("Local posts"), "local"),
    "whatsapp": (_("Official API · chats in the inbox"), "messaging"),
}


def platform_description(platform: str) -> str:
    return _PLATFORMS.get(platform, ("", "other"))[0]


def platform_category(platform: str) -> str:
    return _PLATFORMS.get(platform, ("", "other"))[1]
