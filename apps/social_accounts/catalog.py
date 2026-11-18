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
    ("ads", _("Ads")),
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


# What a person should know before the platform's own window opens: who has to
# be signed in, what to allow, what comes back. Written for someone who has
# never connected an account. ``note`` is the one requirement that most often
# makes the connection fail. A platform without an entry connects straight
# away (the ones with their own form already explain themselves there).
_SIGN_IN_AND_ALLOW = _("Allow the access we ask for")
_KEEP_PERMISSIONS = _("Keep every permission ticked: each one powers a feature, and leaving one out breaks it later.")

_CONNECT_GUIDES = {
    "facebook": {
        "steps": (
            (
                _("Sign in with Facebook"),
                _("Use the Facebook profile that manages the Page, with full control or permission to create content."),
            ),
            (
                _SIGN_IN_AND_ALLOW,
                _("When Facebook asks, select the Pages you want to use and keep every permission ticked."),
            ),
            (
                _("Choose the Pages to connect"),
                _("We list the Pages that login reaches, and you pick which ones enter this brand."),
            ),
        ),
        "note": _("Only Pages can be connected. Facebook does not let apps publish to a personal profile."),
    },
    "instagram": {
        "steps": (
            (
                _("Sign in with Facebook"),
                _(
                    "Instagram connects through the Facebook profile that manages the Page linked to the Instagram account."
                ),
            ),
            (
                _SIGN_IN_AND_ALLOW,
                _("Select both the Instagram account and its Facebook Page, and keep every permission ticked."),
            ),
            (
                _("Choose the Instagram accounts"),
                _("We list the accounts that login reaches, and you pick which ones to connect."),
            ),
        ),
        "note": _(
            "The Instagram account has to be professional (Business or Creator) and linked to a Facebook Page. "
            "Personal accounts cannot be connected: switch the account type in the Instagram app first."
        ),
    },
    "instagram_login": {
        "steps": (
            (_("Sign in with Instagram"), _("Use the Instagram account itself. No Facebook Page is needed.")),
            (_SIGN_IN_AND_ALLOW, _KEEP_PERMISSIONS),
            (_("Done"), _("The account appears here, ready to publish and to receive comments and messages.")),
        ),
        "note": _(
            "The account has to be professional (Business or Creator). "
            "Personal accounts cannot be connected: switch the account type in the Instagram app first."
        ),
    },
    "threads": {
        "steps": (
            (_("Sign in with Threads"), _("Use the Instagram login of the Threads profile you want to connect.")),
            (_SIGN_IN_AND_ALLOW, _KEEP_PERMISSIONS),
            (_("Done"), _("The profile appears here, ready to publish and to receive replies.")),
        ),
        "note": "",
    },
    "whatsapp": {
        "steps": (
            (
                _("Sign in with your Meta account"),
                _("Use the Facebook profile that administers the company's business portfolio on Meta."),
            ),
            (
                _("Choose the WhatsApp account and the number"),
                _(
                    "Pick or create the WhatsApp Business account and the phone number. Meta confirms the number by SMS or call."
                ),
            ),
            (_("Done"), _("Messages sent to that number start arriving in the inbox.")),
        ),
        "note": _(
            "A number already in use in the WhatsApp or WhatsApp Business app cannot be connected yet. "
            "Use a number that is not registered in the app."
        ),
    },
    "linkedin_personal": {
        "steps": (
            (_("Sign in with LinkedIn"), _("Use the LinkedIn account of the profile you want to publish to.")),
            (_SIGN_IN_AND_ALLOW, _KEEP_PERMISSIONS),
            (_("Done"), _("Your profile appears here, ready to publish.")),
        ),
        "note": "",
    },
    "linkedin_company": {
        "steps": (
            (_("Sign in with LinkedIn"), _("Use a LinkedIn account that is a super admin of the company page.")),
            (_SIGN_IN_AND_ALLOW, _KEEP_PERMISSIONS),
            (
                _("Choose the pages to connect"),
                _("We list the company pages you administer, and you pick which ones to connect."),
            ),
        ),
        "note": _("Other roles on the page, such as content admin or analyst, are not enough to connect it."),
    },
    "google_business": {
        "steps": (
            (
                _("Sign in with Google"),
                _("Use the Google account that is owner or manager of the Business Profile."),
            ),
            (_SIGN_IN_AND_ALLOW, _KEEP_PERMISSIONS),
            (
                _("Choose the locations"),
                _("We list the locations that account manages, and you pick which ones to connect."),
            ),
        ),
        "note": _("The Business Profile has to be verified by Google before it accepts posts."),
    },
    "youtube": {
        "steps": (
            (_("Sign in with Google"), _("Use the Google account that owns or manages the channel.")),
            (
                _("Choose the channel"),
                _(
                    "If the account has more than one channel, Google asks which one. Then keep every permission ticked."
                ),
            ),
            (_("Done"), _("The channel appears here, ready to receive videos and Shorts.")),
        ),
        "note": "",
    },
    "tiktok": {
        "steps": (
            (_("Sign in with TikTok"), _("Use the TikTok account you want to publish to.")),
            (_SIGN_IN_AND_ALLOW, _("Leave every option switched on in the TikTok window.")),
            (_("Done"), _("The account appears here, ready to receive videos.")),
        ),
        "note": "",
    },
    "pinterest": {
        "steps": (
            (_("Sign in with Pinterest"), _("Use the Pinterest account that owns the boards you want to publish to.")),
            (_SIGN_IN_AND_ALLOW, _KEEP_PERMISSIONS),
            (_("Done"), _("The account appears here, and you choose the board when creating each Pin.")),
        ),
        "note": "",
    },
    "x": {
        "steps": (
            (_("Sign in with X"), _("Use the X account you want to publish to.")),
            (_SIGN_IN_AND_ALLOW, _("Authorize the app in the X window.")),
            (_("Done"), _("The account appears here, ready to publish.")),
        ),
        "note": "",
    },
}

# Meta Ads is not a SocialAccount (apps.ads), but it is offered on the same page
# and opens with the same guide.
_CONNECT_GUIDES["meta_ads"] = {
    "steps": (
        (
            _("Sign in with Facebook"),
            _(
                "Use the Facebook profile that has access to the ad account in Meta Business Suite, "
                "as admin or advertiser."
            ),
        ),
        (
            _SIGN_IN_AND_ALLOW,
            _(
                "When Meta asks, select the ad account, its Pixel and the Facebook Page, and keep every permission ticked."
            ),
        ),
        (
            _("Choose the ad account, the Pixel and the Page"),
            _(
                "Back here, pick the ad account the campaigns run in, the Pixel that measures your site "
                "and the Page the ads are published as."
            ),
        ),
    ),
    "note": _(
        "The ad account has to be active and have a payment method in Meta. Sales and lead campaigns also "
        "need the Pixel installed on your site, and every ad needs a Facebook Page."
    ),
}

# Connected by a form of our own, or by the platform's embedded window, rather
# than by sending the person to the platform to sign in and come back.
_FORM_FLOW_PLATFORMS = frozenset({"bluesky", "mastodon", "devto"})


def connect_guide(platform: str) -> dict | None:
    """The steps and the main requirement to show before connecting, if any."""
    return _CONNECT_GUIDES.get(platform)


def connect_flow(platform: str) -> str:
    """How the connection starts: ``oauth``, ``form`` or ``whatsapp``."""
    if platform == "whatsapp":
        return "whatsapp"
    return "form" if platform in _FORM_FLOW_PLATFORMS else "oauth"
