"""X (formerly Twitter) API v2 provider.

OAuth 2.0 Authorization Code with PKCE (confidential client: the token calls
carry HTTP Basic auth with the app's client id and secret). Access tokens live
two hours; ``offline.access`` returns a refresh token, which X rotates on every
refresh.

Publishing a post needs a paid API tier with write access (Basic or higher).
Media goes through the v2 upload endpoints before the post references it by
id: a photo in one request, a video or GIF through the chunked
initialize/append/finalize protocol, then polled until X finishes processing.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from .base import SocialProvider
from .exceptions import OAuthError, ProviderError, PublishError, RateLimitError
from .types import (
    AccountMetrics,
    AccountProfile,
    AuthType,
    MediaType,
    OAuthTokens,
    PostMetrics,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://x.com/i/oauth2/authorize"
API_BASE = "https://api.x.com/2"
TOKEN_URL = f"{API_BASE}/oauth2/token"
REVOKE_URL = f"{API_BASE}/oauth2/revoke"
MEDIA_UPLOAD_URL = f"{API_BASE}/media/upload"

# A post takes up to four photos, or a single video or GIF.
X_MAX_IMAGES = 4
# Chunk size for the video upload; X accepts segments up to 5 MB.
X_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024
# How long to wait for X to finish processing an uploaded video.
X_MEDIA_PROCESSING_TIMEOUT_SEC = 300
X_MEDIA_STATUS_INTERVAL_SEC = 3

_MIME_BY_TYPE = {
    "image": "image/jpeg",
    "gif": "image/gif",
    "video": "video/mp4",
}


def _pkce_code_challenge(code_verifier: str) -> str:
    """RFC 7636 S256 challenge: base64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class XProvider(SocialProvider):
    """X API v2 provider (OAuth 2.0 user context)."""

    uses_pkce = True

    # X only takes media as uploaded bytes, never by URL.
    needs_local_media = True

    # X reports public metrics as lifetime totals.
    account_metrics_supports_date_range = False

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "X"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return 280

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO, PostType.CAROUSEL]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP, MediaType.MP4, MediaType.MOV]

    @property
    def required_scopes(self) -> list[str]:
        return ["tweet.read", "tweet.write", "users.read", "media.write", "offline.access"]

    @property
    def rate_limits(self) -> RateLimitConfig:
        # The Basic tier allows on the order of 100 posts per user per day.
        return RateLimitConfig(requests_per_hour=300, requests_per_day=2000, publish_per_day=100)

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def _error_for_response(self, response: httpx.Response) -> ProviderError:
        """Use X's ``x-rate-limit-reset`` (epoch seconds) as the retry delay on 429."""
        if response.status_code == 429:
            reset = response.headers.get("x-rate-limit-reset")
            retry_after = None
            if reset and reset.isdigit():
                retry_after = max(int(reset) - int(time.time()), 1)
            logger.error("X API request was rate limited (HTTP 429)")
            return RateLimitError(
                f"Rate limit exceeded for X: {response.text[:500]}",
                retry_after=retry_after,
                platform=self.platform_name,
                raw_response=self._safe_json(response),
            )
        return super()._error_for_response(response)

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def _basic_auth_header(self) -> dict[str, str]:
        raw = f"{self.credentials['client_id']}:{self.credentials['client_secret']}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        if not code_verifier:
            raise OAuthError("X requires PKCE (no code_verifier supplied)", platform=self.platform_name)
        params = {
            "response_type": "code",
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.required_scopes),
            "state": state,
            "code_challenge": _pkce_code_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def _token_request(self, data: dict) -> OAuthTokens:
        body = self._request("POST", TOKEN_URL, headers=self._basic_auth_header(), data=data).json()
        if "access_token" not in body:
            raise OAuthError("X token request failed", platform=self.platform_name, raw_response=body)
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier or "",
                "client_id": self.credentials["client_id"],
            }
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.credentials["client_id"],
            }
        )

    def revoke_token(self, access_token: str) -> bool:
        try:
            self._request(
                "POST",
                REVOKE_URL,
                headers=self._basic_auth_header(),
                data={"token": access_token, "token_type_hint": "access_token"},
            )
        except ProviderError:
            logger.warning("X token revocation failed", exc_info=True)
            return False
        return True

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        data = (
            self._request(
                "GET",
                f"{API_BASE}/users/me",
                access_token=access_token,
                params={"user.fields": "profile_image_url,public_metrics"},
            )
            .json()
            .get("data", {})
        )
        if not data.get("id"):
            raise OAuthError("X did not return the connected user", platform=self.platform_name)
        return AccountProfile(
            platform_id=data["id"],
            name=data.get("name") or data.get("username", ""),
            handle=data.get("username"),
            avatar_url=data.get("profile_image_url"),
            follower_count=int((data.get("public_metrics") or {}).get("followers_count") or 0),
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        text = content.text or ""
        if len(text) > self.max_caption_length:
            raise PublishError(
                f"Post text exceeds {self.max_caption_length} characters (got {len(text)})",
                platform=self.platform_name,
                retryable=False,
            )

        media_ids = self._upload_media(access_token, content)
        if not text and not media_ids:
            raise PublishError("An X post needs text or media", platform=self.platform_name, retryable=False)

        payload: dict = {}
        if text:
            payload["text"] = text
        if media_ids:
            payload["media"] = {"media_ids": media_ids}

        data = self._request("POST", f"{API_BASE}/tweets", access_token=access_token, json=payload).json()
        post_id = (data.get("data") or {}).get("id")
        if not post_id:
            raise PublishError("X did not return the post id", platform=self.platform_name, raw_response=data)

        handle = content.extra.get("account_handle")
        url = f"https://x.com/{handle}/status/{post_id}" if handle else f"https://x.com/i/web/status/{post_id}"
        return PublishResult(platform_post_id=post_id, url=url, extra={"post_id": post_id})

    def _media_kind(self, content: PublishContent, index: int) -> str:
        """ "image", "gif" or "video", from the media library's sniffed type when known."""
        kind = content.media_types[index] if index < len(content.media_types) else ""
        if kind in _MIME_BY_TYPE:
            return kind
        path = content.media_files[index].lower()
        if path.endswith(".gif"):
            return "gif"
        return "video" if content.is_video(index) else "image"

    def _upload_media(self, access_token: str, content: PublishContent) -> list[str]:
        files = content.media_files
        if not files:
            return []
        kinds = [self._media_kind(content, i) for i in range(len(files))]
        if any(kind in ("video", "gif") for kind in kinds) and len(files) > 1:
            raise PublishError(
                "An X post takes either up to 4 photos or a single video or GIF",
                platform=self.platform_name,
                retryable=False,
            )
        if len(files) > X_MAX_IMAGES:
            raise PublishError(
                f"An X post takes at most {X_MAX_IMAGES} photos",
                platform=self.platform_name,
                retryable=False,
            )
        media_ids = []
        for path, kind in zip(files, kinds, strict=True):
            if kind == "image":
                media_ids.append(self._upload_image(access_token, path))
            else:
                media_ids.append(self._upload_chunked(access_token, path, kind, content))
        return media_ids

    def _upload_image(self, access_token: str, path: str) -> str:
        with open(path, "rb") as fh:
            data = self._request(
                "POST",
                MEDIA_UPLOAD_URL,
                access_token=access_token,
                data={"media_category": "tweet_image"},
                files={"media": (os.path.basename(path), fh)},
            ).json()
        return self._media_id(data, "upload the photo")

    def _upload_chunked(self, access_token: str, path: str, kind: str, content: PublishContent) -> str:
        total = os.path.getsize(path)
        init = self._request(
            "POST",
            f"{MEDIA_UPLOAD_URL}/initialize",
            access_token=access_token,
            json={
                "media_type": _MIME_BY_TYPE[kind],
                "total_bytes": total,
                "media_category": "tweet_gif" if kind == "gif" else "tweet_video",
            },
        ).json()
        media_id = self._media_id(init, "start the upload")

        with open(path, "rb") as fh:
            segment = 0
            while chunk := fh.read(X_UPLOAD_CHUNK_BYTES):
                self._request(
                    "POST",
                    f"{MEDIA_UPLOAD_URL}/{media_id}/append",
                    access_token=access_token,
                    data={"segment_index": str(segment)},
                    files={"media": ("chunk", chunk)},
                )
                segment += 1

        final = self._request("POST", f"{MEDIA_UPLOAD_URL}/{media_id}/finalize", access_token=access_token).json()
        processing = (final.get("data") or {}).get("processing_info")
        if processing:
            self._wait_for_processing(access_token, media_id, processing, content)
        return media_id

    def _wait_for_processing(self, access_token: str, media_id: str, processing: dict, content: PublishContent):
        """Poll the upload STATUS until X has finished processing the video or GIF."""
        state = {"info": processing}

        def check() -> bool:
            info = state["info"]
            if info.get("state") == "failed":
                error = (info.get("error") or {}).get("message") or "X could not process the video"
                raise PublishError(error, platform=self.platform_name, retryable=False)
            if info.get("state") == "succeeded":
                return True
            status = self._request(
                "GET",
                MEDIA_UPLOAD_URL,
                access_token=access_token,
                params={"command": "STATUS", "media_id": media_id},
            ).json()
            state["info"] = (status.get("data") or {}).get("processing_info") or {"state": "succeeded"}
            return state["info"].get("state") == "succeeded"

        self._poll_until(
            check,
            deadline=time.monotonic() + X_MEDIA_PROCESSING_TIMEOUT_SEC,
            interval=X_MEDIA_STATUS_INTERVAL_SEC,
            on_timeout=lambda: PublishError("X took too long to process the video", platform=self.platform_name),
            should_stop=content.should_stop,
        )

    def _media_id(self, data: dict, action: str) -> str:
        media_id = (data.get("data") or {}).get("id")
        if not media_id:
            raise PublishError(f"X failed to {action}", platform=self.platform_name, raw_response=data)
        return media_id

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        data = (
            self._request(
                "GET",
                f"{API_BASE}/tweets/{post_id}",
                access_token=access_token,
                params={"tweet.fields": "public_metrics"},
            )
            .json()
            .get("data", {})
        )
        m = data.get("public_metrics") or {}
        likes = int(m.get("like_count") or 0)
        replies = int(m.get("reply_count") or 0)
        reposts = int(m.get("retweet_count") or 0) + int(m.get("quote_count") or 0)
        bookmarks = int(m.get("bookmark_count") or 0)
        return PostMetrics(
            impressions=int(m.get("impression_count") or 0),
            likes=likes,
            comments=replies,
            shares=reposts,
            saves=bookmarks,
            engagements=likes + replies + reposts + bookmarks,
            extra={"public_metrics": m},
        )

    def get_account_metrics(self, access_token: str, date_range: tuple[datetime, datetime]) -> AccountMetrics:
        data = (
            self._request(
                "GET",
                f"{API_BASE}/users/me",
                access_token=access_token,
                params={"user.fields": "public_metrics"},
            )
            .json()
            .get("data", {})
        )
        followers = int((data.get("public_metrics") or {}).get("followers_count") or 0)
        return AccountMetrics(followers=followers, extra={"fetched_at": datetime.now(UTC).isoformat()})
