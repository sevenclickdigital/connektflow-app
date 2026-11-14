"""Instagram mentions and tags in the inbox.

The ``mentions`` webhook names a mention only by id, so its text is fetched
afterwards. A mention sits on media another account owns and is answered on
the account's ``/mentions`` edge. A tag has no webhook and cannot be answered.
"""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import InboxMessage
from apps.inbox.services import _dispatch_to_platform, fill_instagram_mention, reply_eligibility
from apps.inbox.webhooks import INSTAGRAM_MENTION_PLACEHOLDER
from apps.social_accounts.models import SocialAccount
from providers.exceptions import APIError, MessagingError
from providers.instagram import InstagramProvider


def _resp(data):
    return MagicMock(json=MagicMock(return_value=data))


@pytest.fixture
def ig_account(db, inbox_workspace):
    return SocialAccount.objects.create(
        workspace=inbox_workspace,
        platform="instagram",
        account_platform_id="ig-1",
        account_name="Brand",
        oauth_access_token="tok",
    )


def _mention(account, *, extra, body=INSTAGRAM_MENTION_PLACEHOLDER, message_type=InboxMessage.MessageType.MENTION):
    return InboxMessage.objects.create(
        workspace=account.workspace,
        social_account=account,
        platform_message_id=extra.get("comment_id") or extra.get("media_id"),
        message_type=message_type,
        sender_name="Instagram",
        body=body,
        extra=extra,
        received_at=timezone.now(),
    )


@override_settings(PLATFORM_CREDENTIALS_FROM_ENV={"instagram": {"app_secret": "fb-secret"}})
def test_the_mentions_webhook_queues_reading_the_text(client, ig_account, django_capture_on_commit_callbacks):
    payload = {"entry": [{"id": "ig-1", "changes": [{"field": "mentions", "value": {"media_id": "m-1"}}]}]}
    body = json.dumps(payload).encode()

    with (
        patch("apps.inbox.tasks.fill_instagram_mention") as task,
        django_capture_on_commit_callbacks(execute=True),
    ):
        client.post(
            reverse("inbox_webhooks:webhook_facebook"),
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=" + hmac.new(b"fb-secret", body, hashlib.sha256).hexdigest(),
        )

    message = InboxMessage.objects.get(platform_message_id="m-1")
    task.assert_called_once_with(str(message.id))


def test_a_caption_mention_gets_its_text_author_and_link(ig_account):
    message = _mention(ig_account, extra={"media_id": "m-1", "reply_edge": "media"})
    provider = MagicMock(spec=InstagramProvider)
    provider.get_mention.return_value = {
        "text": "Love this from @brand",
        "username": "ana",
        "timestamp": "",
        "permalink": "https://www.instagram.com/p/x/",
    }

    with patch("apps.inbox.services.get_provider", return_value=provider):
        assert fill_instagram_mention(message.id)

    message.refresh_from_db()
    assert message.body == "Love this from @brand"
    assert (message.sender_name, message.sender_handle) == ("ana", "ana")
    assert message.extra["permalink"] == "https://www.instagram.com/p/x/"
    assert provider.get_mention.call_args.kwargs == {"media_id": "m-1", "comment_id": ""}


def test_a_refused_lookup_keeps_the_placeholder(ig_account):
    message = _mention(ig_account, extra={"media_id": "m-1"})
    provider = MagicMock(spec=InstagramProvider)
    provider.get_mention.side_effect = APIError("comments are off", platform="instagram")

    with patch("apps.inbox.services.get_provider", return_value=provider):
        assert not fill_instagram_mention(message.id)

    message.refresh_from_db()
    assert message.body == INSTAGRAM_MENTION_PLACEHOLDER


def test_a_mention_already_filled_is_left_alone(ig_account):
    message = _mention(ig_account, extra={"media_id": "m-1"}, body="Already here")
    provider = MagicMock(spec=InstagramProvider)

    with patch("apps.inbox.services.get_provider", return_value=provider):
        assert not fill_instagram_mention(message.id)

    provider.get_mention.assert_not_called()


def test_a_mention_reply_is_flagged_for_the_mentions_edge(ig_account):
    message = _mention(ig_account, extra={"media_id": "m-1", "comment_id": "c-1"})
    provider = MagicMock()
    provider.reply_to_comment.return_value = SimpleNamespace(platform_message_id="r-1")

    with patch("apps.inbox.services.get_provider", return_value=provider):
        _dispatch_to_platform(message, "Thanks!")

    assert provider.reply_to_comment.call_args.kwargs["extra"]["mention"] is True


def test_a_tag_cannot_be_answered(ig_account):
    message = _mention(ig_account, extra={"media_id": "m-1"}, message_type=InboxMessage.MessageType.TAG)
    provider = MagicMock()

    assert not reply_eligibility(message)["can_send"]
    with patch("apps.inbox.services.get_provider", return_value=provider), pytest.raises(MessagingError):
        _dispatch_to_platform(message, "Thanks!")
    provider.reply_to_comment.assert_not_called()


class TestInstagramProviderMentions:
    def _provider(self, *responses):
        provider = InstagramProvider({"ig_user_id": "ig-1"})
        provider._request = MagicMock(side_effect=[_resp(r) for r in responses])
        return provider

    def test_a_comment_mention_is_answered_on_the_mentions_edge(self):
        provider = self._provider({"id": "r-1"})

        result = provider.reply_to_comment(
            "tok", "c-1", "Thanks!", extra={"mention": True, "media_id": "m-1", "comment_id": "c-1"}
        )

        method, url = provider._request.call_args.args
        assert (method, url.rsplit("/", 2)[-2:]) == ("POST", ["ig-1", "mentions"])
        assert provider._request.call_args.kwargs["json"] == {
            "media_id": "m-1",
            "comment_id": "c-1",
            "message": "Thanks!",
        }
        assert result.platform_message_id == "r-1"

    def test_a_caption_mention_names_only_the_media(self):
        provider = self._provider({"id": "r-1"})

        provider.reply_to_comment("tok", "m-1", "Thanks!", extra={"mention": True, "media_id": "m-1"})

        assert provider._request.call_args.kwargs["json"] == {"media_id": "m-1", "message": "Thanks!"}

    def test_a_comment_mention_is_read_through_mentioned_comment(self):
        provider = self._provider(
            {"mentioned_comment": {"text": "hi @brand", "media": {"permalink": "https://www.instagram.com/p/x/"}}}
        )

        mention = provider.get_mention("tok", media_id="m-1", comment_id="c-1")

        assert provider._request.call_args.kwargs["params"]["fields"].startswith("mentioned_comment.comment_id(c-1)")
        assert mention == {
            "text": "hi @brand",
            "username": "",
            "timestamp": "",
            "permalink": "https://www.instagram.com/p/x/",
        }

    def test_tagged_posts_arrive_as_tags(self):
        provider = self._provider(
            {
                "data": [
                    {
                        "id": "m-9",
                        "caption": "With @brand",
                        "username": "ana",
                        "timestamp": "2026-11-01T10:00:00+0000",
                        "permalink": "https://www.instagram.com/p/y/",
                    }
                ]
            }
        )

        (message,) = provider._fetch_tagged_media("tok")

        assert provider._request.call_args.args[1].endswith("/ig-1/tags")
        assert (message.platform_message_id, message.message_type) == ("tag_m-9", "tag")
        assert (message.sender_name, message.text) == ("ana", "With @brand")
        assert message.extra["permalink"] == "https://www.instagram.com/p/y/"

    def test_tags_are_not_polled_without_comment_access(self):
        provider = InstagramProvider({"ig_user_id": "ig-1", "missing_scopes": ["instagram_manage_comments"]})
        provider._request = MagicMock()

        assert provider._fetch_tagged_media("tok") == []
        provider._request.assert_not_called()
