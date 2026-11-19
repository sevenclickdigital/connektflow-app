"""Tests for Threads provider OAuth token handling.

Threads issues no separate refresh token: the long-lived access token is its own
refresh credential, replayed against ``th_refresh_token``. The provider therefore
has to return it as ``OAuthTokens.refresh_token`` — every refresh path in the app
skips accounts whose ``oauth_refresh_token`` is empty.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from providers.exceptions import APIError
from providers.threads import LONG_LIVED_TOKEN_TTL, ThreadsProvider

CREDENTIALS = {"app_id": "th-id", "app_secret": "th-sec"}


def _response(body: dict) -> MagicMock:
    return MagicMock(json=MagicMock(return_value=body))


class TestExchangeCode:
    @patch.object(ThreadsProvider, "_request")
    def test_returns_long_lived_token_as_its_own_refresh_credential(self, mock_request):
        mock_request.side_effect = [
            _response({"access_token": "short-lived", "user_id": "1"}),
            _response({"access_token": "long-lived", "expires_in": 5184000, "token_type": "bearer"}),
        ]

        tokens = ThreadsProvider(CREDENTIALS).exchange_code("code", "https://example.com/cb/")

        assert tokens.access_token == "long-lived"
        # Without this the connect callback stores oauth_refresh_token="" and the
        # 60-day token expires unrefreshed. It must be the long-lived token, not
        # the short-lived one the first leg returned.
        assert tokens.refresh_token == "long-lived"
        assert tokens.expires_in == 5184000

    @patch.object(ThreadsProvider, "_request")
    def test_missing_expires_in_falls_back_to_the_60_day_ttl(self, mock_request):
        mock_request.side_effect = [
            _response({"access_token": "short-lived", "user_id": "1"}),
            _response({"access_token": "long-lived"}),
        ]

        tokens = ThreadsProvider(CREDENTIALS).exchange_code("code", "https://example.com/cb/")

        # A None here leaves token_expires_at unset, which keeps the account out
        # of the refresh cycle entirely.
        assert tokens.expires_in == LONG_LIVED_TOKEN_TTL


class TestRefreshToken:
    @patch.object(ThreadsProvider, "_request")
    def test_rotated_token_carries_forward_as_the_next_refresh_credential(self, mock_request):
        mock_request.return_value = _response({"access_token": "rotated", "expires_in": 5184000})

        tokens = ThreadsProvider(CREDENTIALS).refresh_token("current-long-lived")

        assert tokens.access_token == "rotated"
        # Otherwise the account refreshes once, then replays the stale original.
        assert tokens.refresh_token == "rotated"
        assert tokens.expires_in == 5184000

    @patch.object(ThreadsProvider, "_request")
    def test_missing_expires_in_falls_back_to_the_60_day_ttl(self, mock_request):
        mock_request.return_value = _response({"access_token": "rotated"})

        tokens = ThreadsProvider(CREDENTIALS).refresh_token("current-long-lived")

        # Consumers only advance token_expires_at when expires_in is truthy, so a
        # None would leave the stale past expiry in place and re-trigger a
        # refresh on every single health check.
        assert tokens.expires_in == LONG_LIVED_TOKEN_TTL

    @patch.object(ThreadsProvider, "_request")
    def test_replays_the_access_token_against_th_refresh_token(self, mock_request):
        mock_request.return_value = _response({"access_token": "rotated", "expires_in": 5184000})

        ThreadsProvider(CREDENTIALS).refresh_token("current-long-lived")

        _, kwargs = mock_request.call_args
        assert kwargs["params"] == {
            "grant_type": "th_refresh_token",
            "access_token": "current-long-lived",
        }


def _reply(reply_id, minute, **fields):
    return {
        "id": reply_id,
        "text": f"reply {reply_id}",
        "username": "rita",
        "timestamp": f"2026-11-19T22:{minute:02d}:00+0000",
        "root_post": {"id": "P1"},
        "replied_to": {"id": "P1"},
        **fields,
    }


class TestInboxReplies:
    def test_the_sign_in_asks_to_read_replies(self):
        assert "threads_read_replies" in ThreadsProvider(CREDENTIALS).required_scopes

    @patch.object(ThreadsProvider, "_request")
    def test_other_people_s_replies_come_back_as_comments_on_the_post(self, mock_request):
        mock_request.side_effect = [
            _response(
                {
                    "data": [
                        {"id": "P1", "permalink": "https://www.threads.com/@loja/post/abc", "media_type": "TEXT_POST"},
                        {"id": "P2", "media_type": "REPOST_FACADE"},
                    ]
                }
            ),
            _response(
                {
                    "data": [
                        _reply("R3", 30, replied_to={"id": "R1"}, username="joao"),
                        _reply("R2", 20, is_reply_owned_by_me=True),
                        _reply("R1", 10, profile_picture_url="https://cdn.example/rita.jpg"),
                    ]
                }
            ),
        ]

        messages = ThreadsProvider(CREDENTIALS).get_messages("token")

        posts_call, conversation_call = mock_request.call_args_list
        assert posts_call.args[:2] == ("GET", "https://graph.threads.net/v1.0/me/threads")
        assert conversation_call.args[:2] == ("GET", "https://graph.threads.net/v1.0/P1/conversation")
        assert conversation_call.kwargs["params"]["reverse"] == "true"
        assert [m.platform_message_id for m in messages] == ["R3", "R1"]
        nested, direct = messages
        assert (direct.message_type, direct.sender_name, direct.text) == ("comment", "rita", "reply R1")
        assert direct.timestamp == datetime(2026, 11, 19, 22, 10, tzinfo=UTC)
        assert direct.extra["stored_post_id"] == "P1" and direct.extra["parent_id"] == ""
        assert direct.extra["post_permalink_url"] == "https://www.threads.com/@loja/post/abc"
        assert direct.extra["sender_avatar_url"] == "https://cdn.example/rita.jpg"
        assert (nested.sender_name, nested.extra["parent_id"]) == ("joao", "R1")

    @patch.object(ThreadsProvider, "_request")
    def test_a_poll_stops_at_the_replies_it_has_already_read(self, mock_request):
        mock_request.side_effect = [
            _response({"data": [{"id": "P1", "media_type": "TEXT_POST"}]}),
            _response(
                {
                    "data": [_reply("R9", 50), _reply("R1", 5)],
                    "paging": {"cursors": {"after": "next"}},
                }
            ),
        ]

        messages = ThreadsProvider(CREDENTIALS).get_messages("token", since=datetime(2026, 11, 19, 22, 40, tzinfo=UTC))

        assert [m.platform_message_id for m in messages] == ["R9"]
        assert mock_request.call_count == 2

    @patch.object(ThreadsProvider, "_request")
    def test_a_post_that_cannot_be_read_does_not_hide_the_others(self, mock_request):
        posts = _response({"data": [{"id": "P1", "media_type": "TEXT_POST"}, {"id": "P2", "media_type": "TEXT_POST"}]})
        mock_request.side_effect = [posts, APIError("gone", status_code=400), _response({"data": [_reply("R1", 10)]})]

        messages = ThreadsProvider(CREDENTIALS).get_messages("token")

        assert [m.platform_message_id for m in messages] == ["R1"]

    @patch.object(ThreadsProvider, "_request")
    def test_a_refusal_on_every_post_is_raised(self, mock_request):
        posts = _response({"data": [{"id": "P1", "media_type": "TEXT_POST"}]})
        mock_request.side_effect = [posts, APIError("no permission", status_code=403)]

        with pytest.raises(APIError):
            ThreadsProvider(CREDENTIALS).get_messages("token")

    @patch.object(ThreadsProvider, "_request")
    def test_answering_a_reply_publishes_a_reply_under_it(self, mock_request):
        mock_request.side_effect = [
            _response({"id": "U1"}),
            _response({"id": "container"}),
            _response({"id": "R-out"}),
        ]

        result = ThreadsProvider(CREDENTIALS).reply_to_comment("token", "R1", "Temos sim!")

        create = mock_request.call_args_list[1]
        assert create.args[:2] == ("POST", "https://graph.threads.net/v1.0/U1/threads")
        assert create.kwargs["data"] == {"media_type": "TEXT", "text": "Temos sim!", "reply_to_id": "R1"}
        assert result.platform_message_id == "R-out"
