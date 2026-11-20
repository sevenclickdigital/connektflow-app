"""Threads API provider."""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

from .base import SocialProvider
from .exceptions import OAuthError, ProviderError, PublishError
from .meta_history import walk_history
from .types import (
    AccountMetrics,
    AccountProfile,
    AuthType,
    CommentResult,
    ExistingPost,
    InboxMessage,
    MediaType,
    OAuthTokens,
    PostMetrics,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
    ReplyResult,
)

logger = logging.getLogger(__name__)

# How many of the account's latest posts a poll reads the replies of.
REPLY_POSTS_PER_POLL = 15
# Pages of 50 replies read per post in one poll, newest first.
REPLY_PAGES_PER_POST = 4
# Read a little behind the last reply stored, so one that the platform listed
# late is not skipped; the inbox drops what it already has.
REPLY_POLL_OVERLAP = timedelta(minutes=10)
# The profile metrics Threads answers for a date range.
PROFILE_DAY_METRICS = "views,likes,replies,reposts,quotes"
REPLY_FIELDS = (
    "id,text,username,timestamp,permalink,media_type,media_url,thumbnail_url,"
    "root_post,replied_to,is_reply_owned_by_me,profile_picture_url"
)


def reply_timestamp(value) -> datetime | None:
    """A Threads timestamp (``2024-08-07T10:33:16+0000``) in UTC, or None."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S%z").astimezone(UTC)
    except ValueError:
        return None


def inbox_reply(item: dict, post_id: str, post_permalink: str, stamp: datetime | None) -> InboxMessage | None:
    """One reply as an inbox comment, or None for the account's own.

    ``item`` is an entry of a post's conversation or the value of a ``replies``
    webhook: both carry the same reply object, so a reply the webhook delivered
    and the poll reads again is stored once, with the same fields.
    """
    reply_id = str(item.get("id") or "")
    if not reply_id or item.get("is_reply_owned_by_me") or stamp is None:
        return None
    username = str(item.get("username") or "")
    parent_id = str((item.get("replied_to") or {}).get("id") or "")
    attachments = []
    media_url = item.get("media_url") or item.get("thumbnail_url")
    if media_url and item.get("media_type") in ("IMAGE", "VIDEO"):
        attachments.append({"type": item["media_type"].lower(), "url": media_url})
    return InboxMessage(
        platform_message_id=reply_id,
        sender_id=username,
        # Threads names the author only when the profile is public.
        sender_name=username or "Threads user",
        text=str(item.get("text") or ""),
        timestamp=stamp,
        message_type="comment",
        extra={
            "comment_id": reply_id,
            "post_id": post_id,
            "stored_post_id": post_id,
            # Empty for a reply to the post itself, the reply it answers otherwise.
            "parent_id": parent_id if parent_id != post_id else "",
            "post_permalink_url": post_permalink,
            "permalink": str(item.get("permalink") or ""),
            "sender_handle": username,
            "sender_avatar_url": str(item.get("profile_picture_url") or ""),
            "attachments": attachments,
        },
    )


def _existing_post(entry: dict) -> ExistingPost | None:
    """A post of the profile's timeline, or None for a repost or an entry without id."""
    post_id = str(entry.get("id") or "")
    media_type = str(entry.get("media_type") or "")
    if not post_id or media_type == "REPOST_FACADE":
        return None
    media_url = entry.get("media_url") or ""
    return ExistingPost(
        platform_post_id=post_id,
        caption=str(entry.get("text") or "").strip(),
        published_at=reply_timestamp(entry.get("timestamp")),
        permalink=entry.get("permalink") or "",
        media_url=media_url,
        thumbnail_url=entry.get("thumbnail_url") or media_url,
        media_kind={"IMAGE": "image", "VIDEO": "video", "CAROUSEL_ALBUM": "carousel"}.get(media_type, ""),
        extra=entry,
    )


AUTH_URL = "https://www.threads.com/oauth/authorize"
TOKEN_URL = "https://graph.threads.net/oauth/access_token"
API_BASE = "https://graph.threads.net/v1.0"

# Threads ingests media (esp. video) asynchronously: the container must reach
# status FINISHED before it can be published, otherwise threads_publish fails
# with "The requested resource does not exist" (code 24 / media not found).
CONTAINER_POLL_INTERVAL = 3  # seconds
CONTAINER_POLL_TIMEOUT = 120  # seconds of video processing

# Meta issues long-lived Threads tokens with a 60-day TTL. Used as the fallback
# when a token response omits ``expires_in`` — see ``_tokens_from``.
LONG_LIVED_TOKEN_TTL = 60 * 24 * 60 * 60


class ThreadsProvider(SocialProvider):
    """Threads API provider using OAuth 2.0."""

    # Publishes from hosted URLs only — the platform fetches the media
    # itself, so ``PublishContent.media_files`` is never read and the engine
    # can skip downloading the asset to local disk entirely.
    needs_local_media = False

    def __init__(self, credentials: dict | None = None):
        creds = dict(credentials or {})
        # Normalize: accept app_id/app_secret as aliases for client_id/client_secret
        if "app_id" in creds and "client_id" not in creds:
            creds["client_id"] = creds.pop("app_id")
        if "app_secret" in creds and "client_secret" not in creds:
            creds["client_secret"] = creds.pop("app_secret")
        super().__init__(creds)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Threads"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return 500

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO, PostType.CAROUSEL]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.MP4, MediaType.MOV]

    @property
    def required_scopes(self) -> list[str]:
        return [
            "threads_basic",
            "threads_content_publish",
            "threads_manage_insights",
            "threads_manage_replies",
            "threads_read_replies",
        ]

    @property
    def rate_limits(self) -> RateLimitConfig:
        return RateLimitConfig(
            requests_per_hour=200,
            requests_per_day=5000,
            publish_per_day=250,
        )

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": ",".join(self.required_scopes),
            "response_type": "code",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        body = resp.json()
        short_lived_token = body.get("access_token")
        if not short_lived_token:
            raise OAuthError(
                f"Threads token exchange failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )

        # Exchange short-lived token for long-lived token
        long_lived = self._exchange_for_long_lived_token(short_lived_token)
        return long_lived

    def _exchange_for_long_lived_token(self, short_lived_token: str) -> OAuthTokens:
        """Exchange a short-lived token for a long-lived one."""
        resp = self._request(
            "GET",
            f"{API_BASE}/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": self.credentials["client_secret"],
                "access_token": short_lived_token,
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"Threads long-lived token exchange failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return self._tokens_from(body)

    @staticmethod
    def _tokens_from(body: dict) -> OAuthTokens:
        """Build OAuthTokens from a long-lived-token response.

        Threads issues no separate refresh token: the long-lived access token is
        replayed as its own refresh credential (see ``refresh_token``). Returning
        it in ``refresh_token`` is what lets the refresh machinery — which skips
        accounts whose ``oauth_refresh_token`` is empty — ever run, and carries
        the rotated token forward so an account keeps refreshing rather than
        replaying the stale original.

        ``expires_in`` falls back to the documented 60-day TTL: consumers only
        advance ``token_expires_at`` when it is truthy, so a response without it
        would leave a stale past expiry in place and re-trigger a refresh on
        every health check.
        """
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body["access_token"],
            expires_in=body.get("expires_in") or LONG_LIVED_TOKEN_TTL,
            token_type=body.get("token_type", "Bearer"),
            raw_response=body,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh a long-lived Threads token.

        Note: Threads uses the access token itself for refresh (no separate
        refresh_token). The ``refresh_token`` argument here is the current
        long-lived access token.
        """
        resp = self._request(
            "GET",
            f"{API_BASE}/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
                "access_token": refresh_token,
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"Threads token refresh failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return self._tokens_from(body)

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        resp = self._request(
            "GET",
            f"{API_BASE}/me",
            access_token=access_token,
            params={
                "fields": "id,username,name,threads_profile_picture_url,threads_biography",
            },
        )
        body = resp.json()
        return AccountProfile(
            platform_id=body.get("id", ""),
            name=body.get("name", ""),
            handle=body.get("username"),
            avatar_url=body.get("threads_profile_picture_url"),
            extra={"biography": body.get("threads_biography")},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        user_id = content.extra.get("user_id")
        if not user_id:
            # Fetch user_id from profile if not provided
            profile = self.get_profile(access_token)
            user_id = profile.platform_id

        if not user_id:
            raise PublishError(
                "Could not determine Threads user_id",
                platform=self.platform_name,
            )

        if content.post_type == PostType.CAROUSEL:
            return self._publish_carousel(access_token, user_id, content)

        return self._publish_single(access_token, user_id, content)

    def _publish_single(
        self,
        access_token: str,
        user_id: str,
        content: PublishContent,
    ) -> PublishResult:
        """Create and publish a single-item thread (TEXT, IMAGE, or VIDEO)."""
        # Step 1: Create container
        container_payload: dict = {
            "text": (content.text or "")[: self.max_caption_length],
        }

        if content.post_type == PostType.IMAGE and content.media_urls:
            container_payload["media_type"] = "IMAGE"
            container_payload["image_url"] = content.media_urls[0]
        elif content.post_type == PostType.VIDEO and content.media_urls:
            container_payload["media_type"] = "VIDEO"
            container_payload["video_url"] = content.media_urls[0]
        else:
            container_payload["media_type"] = "TEXT"

        # Add reply_to_id if this is a reply
        reply_to = content.extra.get("reply_to_id")
        if reply_to:
            container_payload["reply_to_id"] = reply_to

        create_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads",
            access_token=access_token,
            data=container_payload,
        )
        create_body = create_resp.json()
        creation_id = create_body.get("id")

        if not creation_id:
            raise PublishError(
                # The body goes in raw_response, not the message: publish_error
                # is rendered to users, and a ProviderError message is the one
                # thing the sanitizer passes through verbatim.
                "Threads container creation failed",
                platform=self.platform_name,
                raw_response=create_body,
            )

        # Step 1b: wait for media containers to finish processing before
        # publishing. Text-only threads publish instantly, but image/video
        # containers are ingested asynchronously; publishing too early fails
        # with "media not found".
        if container_payload["media_type"] != "TEXT":
            self._wait_for_container(access_token, creation_id)

        # Step 2: Publish the container
        publish_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads_publish",
            access_token=access_token,
            data={"creation_id": creation_id},
        )
        publish_body = publish_resp.json()
        thread_id = publish_body.get("id", "")
        return PublishResult(
            platform_post_id=thread_id,
            extra=publish_body,
        )

    def _wait_for_container(self, access_token: str, creation_id: str) -> None:
        """Poll a Threads media container until it is FINISHED.

        Threads exposes the ingest state via the container's ``status`` field
        (values: IN_PROGRESS, FINISHED, ERROR, EXPIRED, PUBLISHED). Video
        containers stay IN_PROGRESS while Meta transcodes the upload; calling
        threads_publish before FINISHED returns "media not found".
        """

        def finished() -> bool:
            resp = self._request(
                "GET",
                f"{API_BASE}/{creation_id}",
                access_token=access_token,
                params={"fields": "status,error_message"},
            )
            data = resp.json()
            status = data.get("status", "")
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(
                    f"Threads container failed: {data.get('error_message', status)}",
                    platform=self.platform_name,
                    raw_response=data,
                )
            return status in ("FINISHED", "PUBLISHED")

        self._poll_until(
            finished,
            deadline=time.monotonic() + CONTAINER_POLL_TIMEOUT,
            interval=CONTAINER_POLL_INTERVAL,
            on_timeout=lambda: PublishError("Threads container processing timed out", platform=self.platform_name),
        )

    def _publish_carousel(
        self,
        access_token: str,
        user_id: str,
        content: PublishContent,
    ) -> PublishResult:
        """Create and publish a carousel thread."""
        # Step 1: Create individual item containers
        children_ids: list[str] = []

        for index, url in enumerate(content.media_urls):
            if content.is_video(index):
                media_type = "VIDEO"
                key = "video_url"
            else:
                media_type = "IMAGE"
                key = "image_url"

            item_resp = self._request(
                "POST",
                f"{API_BASE}/{user_id}/threads",
                access_token=access_token,
                data={
                    "media_type": media_type,
                    key: url,
                    "is_carousel_item": "true",
                },
            )
            item_body = item_resp.json()
            item_id = item_body.get("id")
            if not item_id:
                raise PublishError(
                    "Threads carousel item creation failed",
                    platform=self.platform_name,
                    raw_response=item_body,
                )
            # Video children ingest asynchronously; wait before they are
            # referenced by the carousel container.
            if media_type == "VIDEO":
                self._wait_for_container(access_token, item_id)
            children_ids.append(item_id)

        # Step 2: Create carousel container
        carousel_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads",
            access_token=access_token,
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(children_ids),
                "text": (content.text or "")[: self.max_caption_length],
            },
        )
        carousel_body = carousel_resp.json()
        creation_id = carousel_body.get("id")
        if not creation_id:
            raise PublishError(
                "Threads carousel container creation failed",
                platform=self.platform_name,
                raw_response=carousel_body,
            )

        self._wait_for_container(access_token, creation_id)

        # Step 3: Publish
        publish_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads_publish",
            access_token=access_token,
            data={"creation_id": creation_id},
        )
        publish_body = publish_resp.json()
        thread_id = publish_body.get("id", "")
        return PublishResult(
            platform_post_id=thread_id,
            extra=publish_body,
        )

    # ------------------------------------------------------------------
    # Comments (replies)
    # ------------------------------------------------------------------

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Reply to a thread (uses the container flow with reply_to_id)."""
        user_id_resp = self._request(
            "GET",
            f"{API_BASE}/me",
            access_token=access_token,
            params={"fields": "id"},
        )
        user_id = user_id_resp.json().get("id", "")

        # Create reply container
        create_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads",
            access_token=access_token,
            data={
                "media_type": "TEXT",
                "text": text[: self.max_caption_length],
                "reply_to_id": post_id,
            },
        )
        creation_id = create_resp.json().get("id")
        if not creation_id:
            raise PublishError(
                "Threads reply container creation failed",
                platform=self.platform_name,
                raw_response=create_resp.json(),
            )

        # Publish
        publish_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads_publish",
            access_token=access_token,
            data={"creation_id": creation_id},
        )
        reply_id = publish_resp.json().get("id", "")
        return CommentResult(
            platform_comment_id=reply_id,
            extra=publish_resp.json(),
        )

    # ------------------------------------------------------------------
    # Inbox (replies other people leave on the account's posts)
    # ------------------------------------------------------------------

    def get_messages(self, access_token: str, since: datetime | None = None) -> list[InboxMessage]:
        """Replies to the account's recent posts, as inbox comments.

        Threads has no feed of "replies to me", so this reads the account's
        latest posts (``threads_basic``) and each one's conversation
        (``threads_read_replies``), newest reply first, stopping at ``since``.
        A reply to a post older than the latest ``REPLY_POSTS_PER_POLL`` is not
        seen: that bounds a poll to one request for the posts plus one per post.
        The account's own replies are left out. A post that cannot be read is
        skipped; when none can, the refusal is raised.
        """
        floor = since - REPLY_POLL_OVERLAP if since else None
        posts = (
            self._request(
                "GET",
                f"{API_BASE}/me/threads",
                access_token=access_token,
                params={"fields": "id,permalink,media_type", "limit": REPLY_POSTS_PER_POLL},
            )
            .json()
            .get("data", [])
        )
        messages: list[InboxMessage] = []
        read = 0
        refused: ProviderError | None = None
        for post in posts:
            post_id = str(post.get("id") or "")
            if not post_id or post.get("media_type") == "REPOST_FACADE":
                continue
            try:
                messages.extend(self._post_replies(access_token, post_id, str(post.get("permalink") or ""), floor))
                read += 1
            except ProviderError as exc:
                # One post Threads will not open (removed, restricted) must not
                # hide the replies under the others.
                logger.warning("Threads replies of post %s could not be read: %s", post_id, exc)
                refused = exc
        if refused is not None and not read:
            # Every post was refused: that is the permission or the token, and
            # the caller has to know.
            raise refused
        return messages

    def _post_replies(self, access_token: str, post_id: str, post_permalink: str, floor) -> list[InboxMessage]:
        """Every reply under one post that is newer than ``floor``, at any depth."""
        replies: list[InboxMessage] = []
        params = {"fields": REPLY_FIELDS, "reverse": "true", "limit": 50}
        for _page in range(REPLY_PAGES_PER_POST):
            body = self._request(
                "GET", f"{API_BASE}/{post_id}/conversation", access_token=access_token, params=params
            ).json()
            for item in body.get("data", []):
                stamp = reply_timestamp(item.get("timestamp"))
                if floor and stamp and stamp < floor:
                    return replies
                reply = inbox_reply(item, post_id, post_permalink, stamp)
                if reply:
                    replies.append(reply)
            after = ((body.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not body.get("data"):
                break
            params = {**params, "after": after}
        return replies

    def reply_to_comment(self, access_token: str, comment_id: str, text: str, extra: dict | None = None) -> ReplyResult:
        """Answer a reply with a reply of the account's own, under it."""
        result = self.publish_comment(access_token, comment_id, text)
        return ReplyResult(platform_message_id=result.platform_comment_id, extra=result.extra)

    def get_post_preview(self, access_token: str, post_id: str) -> dict | None:
        post = self._request(
            "GET",
            f"{API_BASE}/{post_id}",
            access_token=access_token,
            params={"fields": "id,text,permalink,media_type,media_url,thumbnail_url"},
        ).json()
        image = post.get("thumbnail_url") or (post.get("media_url") if post.get("media_type") == "IMAGE" else "")
        return {
            "caption": post.get("text") or "",
            "image_url": image or "",
            "url": post.get("permalink") or "",
            "product_type": "",
        }

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def list_existing_posts(self, access_token: str, *, limit: int = 100) -> list[ExistingPost]:
        """The profile's own posts, newest first. Reposts of other people's posts are left out."""
        return walk_history(
            self._request,
            url=f"{API_BASE}/me/threads",
            access_token=access_token,
            fields="id,text,timestamp,permalink,media_type,media_url,thumbnail_url",
            to_post=_existing_post,
            limit=limit,
        )

    def _profile_insights(self, access_token: str, metric: str, date_range=None) -> dict[str, dict]:
        """The profile's insights (``threads_manage_insights``), keyed by metric name."""
        params: dict = {"metric": metric}
        if date_range:
            params["since"] = int(date_range[0].timestamp())
            params["until"] = int(date_range[1].timestamp())
        body = self._request("GET", f"{API_BASE}/me/threads_insights", access_token=access_token, params=params).json()
        return {str(item.get("name") or ""): item for item in body.get("data", [])}

    @staticmethod
    def _views_by_day(item: dict | None) -> dict[date, int]:
        """Profile views per day.

        ``views`` is the one profile metric Threads breaks out per day. A day
        there is a Pacific day, named by the date of its ``end_time``, and a
        range that is not aligned to it comes back with the neighbouring day
        too: the value is read by date, never summed over the answer.
        """
        days: dict[date, int] = {}
        for point in (item or {}).get("values") or []:
            stamp = reply_timestamp(point.get("end_time"))
            if stamp:
                days[stamp.date()] = int(point.get("value") or 0)
        return days

    def get_account_metrics(self, access_token: str, date_range: tuple[datetime, datetime]) -> AccountMetrics:
        """The profile's day: views, and the likes, replies, reposts and quotes its posts received.

        The follower total is the profile's current one whatever the range, so
        it is read only for a range that reaches the present.
        """
        start, end = date_range
        insights = self._profile_insights(access_token, PROFILE_DAY_METRICS, date_range)
        totals = {
            name: int((insights.get(name, {}).get("total_value") or {}).get("value") or 0)
            for name in ("likes", "replies", "reposts", "quotes")
        }
        followers = None
        if end >= datetime.now(UTC):
            total = self._profile_insights(access_token, "followers_count").get("followers_count", {})
            value = (total.get("total_value") or {}).get("value")
            followers = int(value) if value is not None else None
        return AccountMetrics(
            followers=followers,
            extra={
                "views": self._views_by_day(insights.get("views")).get(start.date(), 0),
                **totals,
                "raw_insights": insights,
            },
        )

    def get_account_metrics_series(
        self, access_token: str, date_range: tuple[datetime, datetime]
    ) -> dict[str, dict[str, float]]:
        """Daily profile views for the range: the only metric Threads dates day by day."""
        views = self._profile_insights(access_token, "views", date_range).get("views")
        return {"views": {day.isoformat(): float(value) for day, value in self._views_by_day(views).items()}}

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        resp = self._request(
            "GET",
            f"{API_BASE}/{post_id}/insights",
            access_token=access_token,
            params={
                "metric": "views,likes,replies,reposts,quotes",
            },
        )
        body = resp.json()
        data = body.get("data", [])

        metrics: dict[str, int] = {}
        for item in data:
            name = item.get("name", "")
            values = item.get("values", [])
            if values:
                metrics[name] = values[0].get("value", 0)

        return PostMetrics(
            impressions=metrics.get("views", 0),
            likes=metrics.get("likes", 0),
            comments=metrics.get("replies", 0),
            shares=metrics.get("reposts", 0),
            engagements=(
                metrics.get("likes", 0)
                + metrics.get("replies", 0)
                + metrics.get("reposts", 0)
                + metrics.get("quotes", 0)
            ),
            extra={
                "quotes": metrics.get("quotes", 0),
                "raw": data,
            },
        )
