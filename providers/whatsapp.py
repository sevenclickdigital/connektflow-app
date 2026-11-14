"""WhatsApp Business (Cloud API) provider — a messaging channel, not a feed.

Connected through Meta's Embedded Signup: the browser runs ``FB.login`` with
the WhatsApp configuration, Meta hands back an authorization code plus the
WhatsApp Business Account (WABA) and phone number the person picked, and
``exchange_code`` turns the code into a business token for that WABA.

The connected ``SocialAccount`` is the phone number: ``account_platform_id`` is
the phone number id (what every webhook names in ``metadata.phone_number_id``
and what ``/messages`` is sent from) and ``webhook_target_id`` is the WABA (what
the app subscribes to). Callers put both in ``credentials`` as
``phone_number_id`` / ``waba_id``.

Nothing is published here and nothing is polled: the Cloud API has no endpoint
that lists past messages, so webhooks are the only way messages arrive.
"""

from __future__ import annotations

import logging
import secrets

import httpx
from django.utils.translation import gettext as _

from .base import SocialProvider
from .exceptions import APIError, MessagingError, OAuthError, ProviderError, RateLimitError
from .facebook import BASE_URL
from .types import (
    AccountProfile,
    AuthType,
    MediaType,
    OAuthTokens,
    PostType,
    PublishContent,
    PublishResult,
    ReplyResult,
)

logger = logging.getLogger(__name__)

# WhatsApp counts characters, not bytes, for a text message body.
MAX_TEXT_LENGTH = 4096

# Error codes the Cloud API answers a send with that the person sending can act
# on. Everything else stays a generic APIError, routed by the shared handling.
_RATE_LIMIT_CODES = {4, 80007, 130429, 131048, 131056}


def send_error_message(code) -> str:
    if code == 131047:
        return _(
            "More than 24 hours have passed since the customer last wrote. "
            "WhatsApp only delivers approved template messages until they write again."
        )
    if code == 131026:
        return _("WhatsApp could not deliver this message to the customer's number.")
    if code == 131030:
        return _("This number is not in the test recipient list of the WhatsApp app.")
    if code == 133010:
        return _("The WhatsApp number is not registered on the Cloud API. Reconnect it.")
    if code in (131051, 131052, 131053):
        return _("WhatsApp does not accept this kind of message.")
    return ""


class WhatsAppProvider(SocialProvider):
    """WhatsApp Cloud API through the Graph API."""

    def __init__(self, credentials: dict | None = None):
        creds = dict(credentials or {})
        if "app_id" in creds and "client_id" not in creds:
            creds["client_id"] = creds.pop("app_id")
        if "app_secret" in creds and "client_secret" not in creds:
            creds["client_secret"] = creds.pop("app_secret")
        super().__init__(creds)

    @property
    def platform_name(self) -> str:
        return "whatsapp"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return MAX_TEXT_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return []

    @property
    def supported_media_types(self) -> list[MediaType]:
        return []

    @property
    def required_scopes(self) -> list[str]:
        return ["whatsapp_business_management", "whatsapp_business_messaging"]

    @property
    def phone_number_id(self) -> str:
        return str(self.credentials.get("phone_number_id") or "")

    @property
    def waba_id(self) -> str:
        return str(self.credentials.get("waba_id") or "")

    def _error_for_response(self, response: httpx.Response) -> ProviderError:
        body = self._safe_json(response)
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if code in _RATE_LIMIT_CODES:
            return RateLimitError(
                _("WhatsApp is limiting how fast this number can send. Wait a few minutes and try again."),
                platform=self.platform_name,
                raw_response=body,
            )
        message = send_error_message(code)
        if message:
            return MessagingError(
                message,
                status_code=response.status_code,
                platform=self.platform_name,
                raw_response=body,
                retryable=False,
            )
        return super()._error_for_response(response)

    # ------------------------------------------------------------------
    # Connection (Embedded Signup)
    # ------------------------------------------------------------------

    def exchange_code(self, code: str, redirect_uri: str = "", code_verifier: str | None = None) -> OAuthTokens:
        """Trade the Embedded Signup code for a business token.

        Embedded Signup runs in Meta's popup through the JS SDK, so there is no
        redirect URI to repeat: the exchange is sent without one.
        """
        data = self._request(
            "GET",
            f"{BASE_URL}/oauth/access_token",
            params={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "code": code,
            },
        ).json()
        if not data.get("access_token"):
            raise OAuthError("WhatsApp token exchange failed", platform=self.platform_name, raw_response=data)
        return OAuthTokens(
            access_token=data["access_token"],
            expires_in=data.get("expires_in"),
            token_type=data.get("token_type", "Bearer"),
            raw_response=data,
        )

    def waba_phone_number_ids(self, access_token: str, waba_id: str) -> set[str]:
        """The phone numbers this token can reach in ``waba_id``.

        The browser reports which WABA and number were picked; this is how the
        server checks that report against what the token actually grants
        before trusting either id.
        """
        data = self._request(
            "GET",
            f"{BASE_URL}/{waba_id}/phone_numbers",
            access_token=access_token,
            params={"fields": "id", "limit": 100},
        ).json()
        return {str(item.get("id")) for item in data.get("data", []) if item.get("id")}

    def get_phone_number(self, access_token: str) -> dict:
        return self._request(
            "GET",
            f"{BASE_URL}/{self.phone_number_id}",
            access_token=access_token,
            params={"fields": "id,display_phone_number,verified_name,platform_type,quality_rating"},
        ).json()

    def ensure_registered(self, access_token: str) -> None:
        """Register the number on the Cloud API if it is not there yet.

        A number fresh from Embedded Signup cannot send or receive until it is
        registered. Registering sets a two-step verification PIN; a random one
        is used and not kept — the business can reset it in WhatsApp Manager.
        A number already on the Cloud API is left alone, since registering it
        again would fail against the PIN it already has.
        """
        if self.get_phone_number(access_token).get("platform_type") == "CLOUD_API":
            return
        self._request(
            "POST",
            f"{BASE_URL}/{self.phone_number_id}/register",
            access_token=access_token,
            json={"messaging_product": "whatsapp", "pin": f"{secrets.randbelow(10**6):06d}"},
        )

    def get_profile(self, access_token: str) -> AccountProfile:
        phone = self.get_phone_number(access_token)
        avatar = ""
        try:
            profile = self._request(
                "GET",
                f"{BASE_URL}/{self.phone_number_id}/whatsapp_business_profile",
                access_token=access_token,
                params={"fields": "profile_picture_url"},
            ).json()
            avatar = ((profile.get("data") or [{}])[0] or {}).get("profile_picture_url") or ""
        except (APIError, httpx.HTTPError):
            # The picture is decoration; the number itself already answered.
            logger.info("WhatsApp business profile picture unavailable for %s", self.phone_number_id)
        return AccountProfile(
            platform_id=str(phone.get("id") or self.phone_number_id),
            name=phone.get("verified_name") or phone.get("display_phone_number") or "WhatsApp",
            handle=phone.get("display_phone_number") or "",
            avatar_url=avatar,
        )

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        raise APIError(
            "WhatsApp is a messaging channel; it cannot publish posts.",
            platform=self.platform_name,
            retryable=False,
        )

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def reply_to_message(
        self,
        access_token: str,
        message_id: str,
        text: str,
        extra: dict | None = None,
        *,
        human_agent: bool = False,
    ) -> ReplyResult:
        """Send a text message to the customer, quoting the message answered.

        WhatsApp addresses the customer's number (``wa_id``), carried in
        ``extra["recipient_id"]``. ``human_agent`` has no WhatsApp equivalent:
        outside the 24-hour window only templates are delivered, which
        ``reply_policy`` checks before this is called.
        """
        recipient = str((extra or {}).get("recipient_id") or (extra or {}).get("sender_id") or "")
        if not recipient:
            raise MessagingError(_("The original message is missing the customer's number."), platform="whatsapp")
        text = (text or "").strip()
        if not text or len(text) > MAX_TEXT_LENGTH:
            raise MessagingError(
                _("WhatsApp messages must contain text and fit within 4,096 characters."), platform="whatsapp"
            )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"body": text, "preview_url": True},
        }
        if message_id:
            payload["context"] = {"message_id": message_id}
        data = self._request(
            "POST",
            f"{BASE_URL}/{self.phone_number_id}/messages",
            access_token=access_token,
            json=payload,
        ).json()
        sent_id = ((data.get("messages") or [{}])[0] or {}).get("id")
        if not sent_id:
            raise MessagingError(
                _("Delivery could not be confirmed. Check the conversation before retrying."), platform="whatsapp"
            )
        return ReplyResult(platform_message_id=str(sent_id), extra=data)

    def download_media(self, access_token: str, media_id: str, *, max_bytes: int) -> tuple[bytes, str]:
        """Return (content, mime type) of a media file a customer sent.

        The webhook carries only a media id. Its download URL is short-lived and
        answers only with the business token, so the inbox fetches it through
        the server instead of linking to it. Refuses files over ``max_bytes``
        and URLs outside Meta's media hosts.
        """
        from urllib.parse import urlsplit

        meta = self._request("GET", f"{BASE_URL}/{media_id}", access_token=access_token).json()
        url = str(meta.get("url") or "")
        host = urlsplit(url).hostname or ""
        if not url.startswith("https://") or not (host.endswith(".fbsbx.com") or host.endswith(".facebook.com")):
            raise APIError("WhatsApp returned an unexpected media URL.", platform=self.platform_name, retryable=False)
        if int(meta.get("file_size") or 0) > max_bytes:
            raise MessagingError(_("This file is too large to open here."), platform=self.platform_name)
        response = self._request("GET", url, access_token=access_token)
        if len(response.content) > max_bytes:
            raise MessagingError(_("This file is too large to open here."), platform=self.platform_name)
        return response.content, str(meta.get("mime_type") or response.headers.get("content-type") or "")

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    def subscribe_webhooks(self, access_token: str, account_id: str) -> bool:
        """Subscribe the app to the WABA, which delivers the number's messages."""
        if not self.waba_id:
            return False
        data = self._request("POST", f"{BASE_URL}/{self.waba_id}/subscribed_apps", access_token=access_token).json()
        return bool(data.get("success"))

    def unsubscribe_webhooks(self, access_token: str, account_id: str) -> bool:
        if not self.waba_id:
            return False
        data = self._request("DELETE", f"{BASE_URL}/{self.waba_id}/subscribed_apps", access_token=access_token).json()
        return bool(data.get("success"))

    def get_webhook_subscriptions(self, access_token: str, account_id: str) -> list[dict]:
        if not self.waba_id:
            return []
        data = self._request("GET", f"{BASE_URL}/{self.waba_id}/subscribed_apps", access_token=access_token).json()
        return list(data.get("data", []))

    def validate_token(self, access_token: str) -> bool:
        try:
            self.get_phone_number(access_token)
            return True
        except Exception:
            return False
