"""Google Business Profile API provider implementation."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from .base import SocialProvider
from .exceptions import OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    MediaType,
    OAuthTokens,
    PostMetrics,
    PostType,
    PublishContent,
    PublishResult,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ACCOUNTS_API = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFO_API = "https://mybusinessbusinessinformation.googleapis.com/v1"
POSTS_API = "https://mybusiness.googleapis.com/v4"

# locations.list rejects a request without readMask ("Field is required", 400
# INVALID_ARGUMENT) — it has no default field set. Ask for exactly the fields
# get_profile() reads, since every extra field is one the caller must also be
# authorized for.
LOCATION_READ_MASK = "name,title,storefrontAddress,phoneNumbers"

# Gallery categories a profile photo can be filed under (v4 ``LocationAssociation``).
# COVER, PROFILE and LOGO are left out: they replace the profile's identity
# images instead of adding to the gallery.
PROFILE_PHOTO_CATEGORIES = (
    "ADDITIONAL",
    "EXTERIOR",
    "INTERIOR",
    "PRODUCT",
    "AT_WORK",
    "FOOD_AND_DRINK",
    "MENU",
    "COMMON_AREA",
    "ROOMS",
    "TEAMS",
)


class GoogleBusinessProvider(SocialProvider):
    """Google Business Profile provider.

    Uses Google OAuth 2.0.  The ``credentials`` dict must contain:

    - ``client_id``
    - ``client_secret``

    Optional:
    - ``account_id`` – Google Business account ID
    - ``location_id`` – Google Business location ID
    """

    # Publishes from hosted URLs only — the platform fetches the media
    # itself, so ``PublishContent.media_files`` is never read and the engine
    # can skip downloading the asset to local disk entirely.
    needs_local_media = False

    # One grant reaches every location the Google account can manage, and a
    # business with two storefronts must be able to say which; see
    # ``get_user_pages``.
    offers_account_picker = True

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Google Business"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return 1500

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.PROFILE_PHOTO]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG]

    @property
    def required_scopes(self) -> list[str]:
        return ["https://www.googleapis.com/auth/business.manage"]

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        """Build the Google OAuth 2.0 authorization URL."""
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.required_scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        """Exchange an authorization code for Google access/refresh tokens."""
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        data = resp.json()
        if "error" in data:
            raise OAuthError(
                f"Token exchange failed: {data.get('error_description', data['error'])}",
                platform=self.platform_name,
                raw_response=data,
            )
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=data.get("expires_in"),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh an expired Google access token."""
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        data = resp.json()
        if "error" in data:
            raise OAuthError(
                f"Token refresh failed: {data.get('error_description', data['error'])}",
                platform=self.platform_name,
                raw_response=data,
            )
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=refresh_token,  # Google doesn't rotate refresh tokens
            expires_in=data.get("expires_in"),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def revoke_token(self, access_token: str) -> bool:
        """Revoke a Google OAuth token."""
        try:
            self._request(
                "POST",
                REVOKE_URL,
                params={"token": access_token},
            )
            return True
        except Exception:
            logger.exception("Failed to revoke Google token")
            return False

    # ------------------------------------------------------------------
    # Account / location helpers
    # ------------------------------------------------------------------

    def _list_accounts(self, access_token: str) -> list[dict]:
        """Return every Google Business account the grant can reach."""
        resp = self._request(
            "GET",
            f"{ACCOUNTS_API}/accounts",
            access_token=access_token,
        )
        return resp.json().get("accounts", [])

    def _list_locations(self, access_token: str, account_id: str) -> list[dict]:
        """Return every location under ``account_id``."""
        resp = self._request(
            "GET",
            f"{BUSINESS_INFO_API}/{account_id}/locations",
            access_token=access_token,
            params={"readMask": LOCATION_READ_MASK},
        )
        return resp.json().get("locations", [])

    def get_user_pages(self, access_token: str) -> list[dict]:
        """Return every location across every account, for the connect picker.

        Named for the hook ``apps.social_accounts.views.oauth_callback`` looks
        for, which is what routes a platform into the multi-select flow instead
        of the single-account one that silently takes the first result.

        ``id`` is the location resource name ("locations/123"), which is both
        the identity stored on the SocialAccount and the path segment
        ``publish_post`` posts to — so a connected account publishes to the
        location the user picked rather than to whichever one the API happens
        to list first.
        """
        pages: list[dict] = []
        for account in self._list_accounts(access_token):
            account_name = account.get("name", "")
            if not account_name:
                continue
            for loc in self._list_locations(access_token, account_name):
                location_name = loc.get("name", "")
                if not location_name:
                    continue
                address_lines = loc.get("storefrontAddress", {}).get("addressLines", [])
                pages.append(
                    {
                        "id": location_name,
                        "name": loc.get("title") or location_name,
                        # One grant covers every location it can reach, so there
                        # is no per-location token to hand back the way Meta
                        # issues per-Page tokens.
                        "access_token": "",
                        "category": ", ".join(address_lines),
                        "picture": None,
                        "followers_count": 0,
                        "account_id": account_name,
                    }
                )
        return pages

    def _get_account_id(self, access_token: str) -> str:
        """Return the account ID from credentials or by listing accounts."""
        if self.credentials.get("account_id"):
            return self.credentials["account_id"]

        accounts = self._list_accounts(access_token)
        if not accounts:
            raise PublishError(
                "No Google Business accounts found",
                platform=self.platform_name,
            )
        # Return the first account's resource name (e.g. "accounts/123")
        return accounts[0]["name"]

    def _get_location_id(self, access_token: str) -> str:
        """The location to act on: the connected one, or the account's first.

        A connected account carries its location, so neither lookup runs. The
        fallback is only reachable without one — accounts connected before the
        picker existed — and it resolves the account itself, which is why that
        call is here rather than in the caller: asking for it up front spent an
        ``accounts.list`` on every publish that never needed it.
        """
        if self.credentials.get("location_id"):
            return self.credentials["location_id"]

        locations = self._list_locations(access_token, self._get_account_id(access_token))
        if not locations:
            raise PublishError(
                "No locations found for Google Business account",
                platform=self.platform_name,
            )
        return locations[0]["name"]

    def _v4_location_name(self, access_token: str) -> str:
        """The location as the v4 API names it: ``accounts/{a}/locations/{l}``.

        The connected account stores the Business Information name
        (``locations/{l}``), but the v4 ``localPosts`` and ``media`` endpoints
        only route under the owning account. The owner is found by listing the
        grant's accounts; with a single account (the common case) no location
        listing is needed.
        """
        location = self._get_location_id(access_token)
        if location.startswith("accounts/"):
            return location

        accounts = [a["name"] for a in self._list_accounts(access_token) if a.get("name")]
        if len(accounts) == 1:
            return f"{accounts[0]}/{location}"
        for account_name in accounts:
            if any(loc.get("name") == location for loc in self._list_locations(access_token, account_name)):
                return f"{account_name}/{location}"
        raise PublishError(
            "The Google Business location is no longer reachable with this connection",
            platform=self.platform_name,
            retryable=False,
        )

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """The connected location, or the grant's first one when none was picked.

        A connected account carries its location (``location_id``), and that
        is the one read: the health check writes this profile's name over the
        account's, so answering with the first location the grant lists
        renamed every account connected to another one after it. The first
        location is only the answer for a grant that has not chosen yet.
        """
        location_id = self.credentials.get("location_id")
        if location_id:
            resp = self._request(
                "GET",
                f"{BUSINESS_INFO_API}/{location_id}",
                access_token=access_token,
                params={"readMask": LOCATION_READ_MASK},
            )
            return self._location_profile(resp.json(), fallback_id=location_id)

        account_id = self._get_account_id(access_token)
        locations = self._list_locations(access_token, account_id)
        if locations:
            return self._location_profile(locations[0], fallback_id=account_id)
        return AccountProfile(
            platform_id=account_id,
            name=account_id,
            handle=None,
        )

    @staticmethod
    def _location_profile(location: dict, *, fallback_id: str) -> AccountProfile:
        address_lines = location.get("storefrontAddress", {}).get("addressLines", [])
        return AccountProfile(
            platform_id=location.get("name", fallback_id),
            name=location.get("title", location.get("name", "")),
            handle=None,
            extra={
                "address": ", ".join(address_lines),
                "phone": location.get("phoneNumbers", {}).get("primaryPhone", ""),
            },
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Publish a local post to Google Business Profile."""
        if content.text and len(content.text) > self.max_caption_length:
            raise PublishError(
                f"Post text exceeds {self.max_caption_length} characters (got {len(content.text)})",
                platform=self.platform_name,
                retryable=False,
            )

        location_id = self._v4_location_name(access_token)

        if content.post_type == PostType.PROFILE_PHOTO:
            return self._publish_profile_photo(access_token, location_id, content)

        # Determine topic type
        topic_type = content.extra.get("topic_type", "STANDARD")

        body: dict = {
            "languageCode": content.extra.get("language_code", "en"),
            "summary": content.text or "",
            "topicType": topic_type,
        }

        # Attach media
        if content.media_urls:
            body["media"] = [{"mediaFormat": "PHOTO", "sourceUrl": url} for url in content.media_urls]

        # EVENT type extras
        if topic_type == "EVENT" and content.extra.get("event"):
            body["event"] = content.extra["event"]

        # OFFER type extras
        if topic_type == "OFFER" and content.extra.get("offer"):
            body["offer"] = content.extra["offer"]

        resp = self._request(
            "POST",
            f"{POSTS_API}/{location_id}/localPosts",
            access_token=access_token,
            json=body,
        )
        data = resp.json()

        post_name = data.get("name", "")
        return PublishResult(
            platform_post_id=post_name,
            url=data.get("searchUrl"),
            extra=data,
        )

    def _publish_profile_photo(self, access_token: str, location_name: str, content: PublishContent) -> PublishResult:
        """Add one photo to the profile's gallery (``media.create``) instead of posting."""
        if len(content.media_urls) != 1 or content.is_video(0):
            raise PublishError(
                "A Google Business profile photo needs exactly one image",
                platform=self.platform_name,
                retryable=False,
            )
        category = content.extra.get("photo_category") or "ADDITIONAL"
        if category not in PROFILE_PHOTO_CATEGORIES:
            category = "ADDITIONAL"

        body: dict = {
            "mediaFormat": "PHOTO",
            "locationAssociation": {"category": category},
            "sourceUrl": content.media_urls[0],
        }
        if content.text:
            body["description"] = content.text

        data = self._request(
            "POST",
            f"{POSTS_API}/{location_name}/media",
            access_token=access_token,
            json=body,
        ).json()
        media_name = data.get("name")
        if not media_name:
            raise PublishError(
                "Google did not return the uploaded photo",
                platform=self.platform_name,
                raw_response=data,
            )
        return PublishResult(
            platform_post_id=media_name,
            url=data.get("googleUrl"),
            extra={"media_name": media_name},
        )

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        """Fetch metrics for a Google Business local post or profile photo."""
        resp = self._request(
            "GET",
            f"{POSTS_API}/{post_id}",
            access_token=access_token,
        )
        data = resp.json()
        if "/media/" in post_id:
            # A gallery photo reports only how often it was viewed.
            views = int((data.get("insights") or {}).get("viewCount") or 0)
            return PostMetrics(impressions=views, extra={"views": views})
        search_views = 0
        maps_views = 0
        for metric in data.get("searchActionMetrics", []):
            if metric.get("metricType") == "QUERIES_DIRECT":
                search_views += metric.get("value", 0)
        return PostMetrics(
            impressions=search_views + maps_views,
            extra={
                "search_views": search_views,
                "maps_views": maps_views,
                "raw": data,
            },
        )
