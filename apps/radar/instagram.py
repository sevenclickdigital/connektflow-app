"""Radar research on Instagram: watched profiles and hashtags.

Both read public data *through* one of the workspace's Instagram accounts
connected with Facebook Login, which is the only connection Meta serves these
endpoints to:

* Business Discovery (watched profiles) needs only the permissions that
  connection already requests.
* Hashtag search also needs the Instagram Public Content Access feature
  approved in the app's App Review, and Meta counts each distinct hashtag
  against a limit of 30 per account in a rolling seven days.

Nothing read here is stored. A watched profile keeps only its handle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from apps.social_accounts.models import SocialAccount
from providers import get_provider
from providers.exceptions import APIError, OAuthError, ProviderError, RateLimitError, TokenExpiredError

from .models import WatchedProfile

# Instagram handles: letters, digits, periods and underscores, at most 30.
_HANDLE = re.compile(r"^[a-z0-9._]{1,30}$")
# Hashtags in the search are letters, digits and underscores; Meta refuses emoji.
_HASHTAG = re.compile(r"^\w{1,100}$")

HASHTAG_EDGES = {"top": "top_media", "recent": "recent_media"}

# Meta's error codes for the two failures the user can act on.
_META_PERMISSION_CODES = {10, 200}
_META_NO_SUCH_USER_CODE = 110


class InvalidInputError(ValueError):
    """A handle or hashtag the platform would refuse; the message is user-facing."""


def research_accounts(workspace_id):
    """Instagram accounts this workspace can research through, best first."""
    return list(
        SocialAccount.objects.for_workspace(workspace_id)
        .filter(platform="instagram")
        .exclude(connection_status=SocialAccount.ConnectionStatus.DISCONNECTED)
        .order_by("account_name")
    )


def clean_handle(value: str) -> str:
    handle = (value or "").strip().lstrip("@").lower()
    if not _HANDLE.match(handle):
        raise InvalidInputError(_("Type an Instagram username, such as @brand."))
    return handle


def clean_hashtag(value: str) -> str:
    tag = (value or "").strip().lstrip("#").lower()
    if not _HASHTAG.match(tag):
        raise InvalidInputError(_("Type one hashtag with letters, numbers or underscores, without spaces or emoji."))
    return tag


def watch_profile(workspace, username: str, user) -> WatchedProfile:
    profile, _created = WatchedProfile.objects.get_or_create(
        workspace=workspace, username=clean_handle(username), defaults={"added_by": user}
    )
    return profile


def _for_display(media: list[dict]) -> list[dict]:
    """Add ``posted_at`` (a datetime) and ``preview_url`` (an image, or empty) to each post.

    A video's ``media_url`` is the video itself, so only its thumbnail can be
    shown; hashtag results carry no thumbnail at all.
    """
    return [
        {
            **m,
            "posted_at": parse_datetime(m.get("timestamp") or ""),
            "preview_url": m.get("thumbnail_url") or ("" if m.get("media_type") == "VIDEO" else m.get("media_url", "")),
        }
        for m in media
    ]


def _provider(account: SocialAccount):
    from apps.publisher.engine import _resolve_publish_credentials

    return get_provider(account.platform, _resolve_publish_credentials(account))


@dataclass
class ProfileReport:
    username: str
    name: str = ""
    biography: str = ""
    website: str = ""
    picture_url: str = ""
    followers: int | None = None
    follows: int | None = None
    posts: int | None = None
    media: list[dict] = field(default_factory=list)
    # Mean of likes plus comments over the posts read, and that over followers.
    avg_interactions: int | None = None
    engagement_rate: float | None = None


def profile_report(account: SocialAccount, username: str) -> ProfileReport:
    data = _provider(account).business_discovery(account.oauth_access_token, clean_handle(username))
    media = _for_display(data.get("media") or [])
    report = ProfileReport(
        username=data.get("username") or username,
        name=data.get("name") or "",
        biography=data.get("biography") or "",
        website=data.get("website") or "",
        picture_url=data.get("profile_picture_url") or "",
        followers=data.get("followers_count"),
        follows=data.get("follows_count"),
        posts=data.get("media_count"),
        media=media,
    )
    # Owners can hide like counts; those posts are left out of the mean rather
    # than counted as zero.
    totals = [m["like_count"] + m.get("comments_count", 0) for m in media if "like_count" in m]
    if totals:
        report.avg_interactions = round(sum(totals) / len(totals))
        if report.followers:
            report.engagement_rate = round(100 * report.avg_interactions / report.followers, 2)
    return report


def hashtag_posts(account: SocialAccount, hashtag: str, *, order: str = "top") -> list[dict]:
    edge = HASHTAG_EDGES.get(order, HASHTAG_EDGES["top"])
    return _for_display(_provider(account).hashtag_media(account.oauth_access_token, clean_hashtag(hashtag), edge=edge))


def _meta_error_code(exc: ProviderError) -> int | None:
    error = (getattr(exc, "raw_response", None) or {}).get("error") or {}
    code = error.get("code")
    return code if isinstance(code, int) else None


def describe_error(exc: ProviderError, *, kind: str) -> str:
    """What to tell the user about a refused research request.

    ``kind`` is ``"profile"`` or ``"hashtag"``; the same Meta code means a
    different thing to fix for each.
    """
    if isinstance(exc, TokenExpiredError | OAuthError):
        return _("The Instagram connection has expired. Reconnect it in Social Accounts.")
    if isinstance(exc, RateLimitError):
        return _("Instagram is limiting requests right now. Wait a few minutes and try again.")
    code = _meta_error_code(exc)
    if kind == "hashtag" and (code in _META_PERMISSION_CODES or isinstance(exc, APIError) and exc.status_code == 403):
        return _(
            "Hashtag search isn't enabled for this app on Meta yet: it needs the "
            "Instagram Public Content Access feature approved in App Review."
        )
    if kind == "profile" and code == _META_NO_SUCH_USER_CODE:
        return _("Instagram found no business or creator account with this username. Personal accounts can't be read.")
    if kind == "profile" and code in _META_PERMISSION_CODES:
        return _("Reconnect Instagram and allow access to insights to read other profiles.")
    return _("Instagram could not answer right now. Try again in a moment.")
