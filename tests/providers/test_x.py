"""Tests for the X (v2 API) provider."""

import base64
import hashlib
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from providers.exceptions import OAuthError, PublishError, RateLimitError
from providers.types import PostType, PublishContent
from providers.x import API_BASE, MEDIA_UPLOAD_URL, TOKEN_URL, XProvider


def _resp(payload):
    return MagicMock(json=MagicMock(return_value=payload))


def _provider():
    return XProvider({"client_id": "cid", "client_secret": "secret"})


def test_auth_url_sends_an_s256_pkce_challenge_and_the_publishing_scopes():
    url = _provider().get_auth_url("https://app.example/cb/x/", "state-1", code_verifier="verifier-123")

    query = parse_qs(urlparse(url).query)
    expected = base64.urlsafe_b64encode(hashlib.sha256(b"verifier-123").digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [expected]
    assert query["code_challenge_method"] == ["S256"]
    assert set(query["scope"][0].split()) == {
        "tweet.read",
        "tweet.write",
        "users.read",
        "media.write",
        "offline.access",
    }


def test_auth_url_refuses_to_build_without_a_verifier():
    with pytest.raises(OAuthError):
        _provider().get_auth_url("https://app.example/cb/x/", "state-1")


def test_code_exchange_replays_the_verifier_with_basic_auth():
    provider = _provider()
    provider._request = MagicMock(return_value=_resp({"access_token": "at", "refresh_token": "rt", "expires_in": 7200}))

    tokens = provider.exchange_code("code-1", "https://app.example/cb/x/", code_verifier="verifier-123")

    assert tokens.access_token == "at" and tokens.refresh_token == "rt"
    args, kwargs = provider._request.call_args
    assert args == ("POST", TOKEN_URL)
    assert kwargs["headers"]["Authorization"] == "Basic " + base64.b64encode(b"cid:secret").decode()
    assert kwargs["data"]["code_verifier"] == "verifier-123"
    assert kwargs["data"]["grant_type"] == "authorization_code"


def test_profile_reads_handle_and_followers():
    provider = _provider()
    provider._request = MagicMock(
        return_value=_resp(
            {"data": {"id": "42", "name": "Shop", "username": "shop", "public_metrics": {"followers_count": 7}}}
        )
    )

    profile = provider.get_profile("at")

    assert (profile.platform_id, profile.handle, profile.follower_count) == ("42", "shop", 7)


def test_text_post_is_published_with_its_url(tmp_path):
    provider = _provider()
    provider._request = MagicMock(return_value=_resp({"data": {"id": "999"}}))

    result = provider.publish_post(
        "at", PublishContent(text="Hello X", post_type=PostType.TEXT, extra={"account_handle": "shop"})
    )

    assert result.platform_post_id == "999"
    assert result.url == "https://x.com/shop/status/999"
    provider._request.assert_called_once_with("POST", f"{API_BASE}/tweets", access_token="at", json={"text": "Hello X"})


def test_a_photo_is_uploaded_then_attached(tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"jpeg-bytes")
    provider = _provider()
    provider._request = MagicMock(side_effect=[_resp({"data": {"id": "m1"}}), _resp({"data": {"id": "999"}})])

    provider.publish_post(
        "at",
        PublishContent(text="pic", media_files=[str(photo)], media_types=["image"], post_type=PostType.IMAGE),
    )

    upload, post = provider._request.call_args_list
    assert upload.args == ("POST", MEDIA_UPLOAD_URL)
    assert upload.kwargs["data"] == {"media_category": "tweet_image"}
    assert post.kwargs["json"] == {"text": "pic", "media": {"media_ids": ["m1"]}}


def test_a_video_goes_through_the_chunked_upload_and_waits_for_processing(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)
    provider = _provider()
    provider._request = MagicMock(
        side_effect=[
            _resp({"data": {"id": "m9"}}),
            _resp({}),
            _resp({"data": {"id": "m9", "processing_info": {"state": "pending"}}}),
            _resp({"data": {"processing_info": {"state": "succeeded"}}}),
            _resp({"data": {"id": "999"}}),
        ]
    )

    provider.publish_post(
        "at",
        PublishContent(text="", media_files=[str(video)], media_types=["video"], post_type=PostType.VIDEO),
    )

    calls = provider._request.call_args_list
    assert calls[0].args == ("POST", f"{MEDIA_UPLOAD_URL}/initialize")
    assert calls[0].kwargs["json"] == {"media_type": "video/mp4", "total_bytes": 10, "media_category": "tweet_video"}
    assert calls[1].args == ("POST", f"{MEDIA_UPLOAD_URL}/m9/append")
    assert calls[2].args == ("POST", f"{MEDIA_UPLOAD_URL}/m9/finalize")
    assert calls[3].kwargs["params"] == {"command": "STATUS", "media_id": "m9"}
    assert calls[4].kwargs["json"] == {"media": {"media_ids": ["m9"]}}


def test_a_video_cannot_share_a_post_with_other_media(tmp_path):
    files = []
    for name in ("v.mp4", "a.jpg"):
        f = tmp_path / name
        f.write_bytes(b"1")
        files.append(str(f))
    provider = _provider()
    provider._request = MagicMock()

    with pytest.raises(PublishError) as exc:
        provider.publish_post(
            "at",
            PublishContent(text="", media_files=files, media_types=["video", "image"], post_type=PostType.CAROUSEL),
        )

    assert exc.value.retryable is False
    provider._request.assert_not_called()


def test_text_over_280_characters_is_refused_before_any_call():
    provider = _provider()
    provider._request = MagicMock()

    with pytest.raises(PublishError):
        provider.publish_post("at", PublishContent(text="a" * 281, post_type=PostType.TEXT))

    provider._request.assert_not_called()


def test_a_429_carries_the_reset_window_as_retry_after(monkeypatch):
    monkeypatch.setattr("providers.x.time.time", lambda: 1000)
    response = httpx.Response(429, headers={"x-rate-limit-reset": "1060"}, json={"title": "Too Many Requests"})

    error = _provider()._error_for_response(response)

    assert isinstance(error, RateLimitError)
    assert error.retry_after == 60


def test_post_metrics_map_public_metrics():
    provider = _provider()
    provider._request = MagicMock(
        return_value=_resp(
            {
                "data": {
                    "public_metrics": {
                        "impression_count": 500,
                        "like_count": 10,
                        "reply_count": 2,
                        "retweet_count": 3,
                        "quote_count": 1,
                        "bookmark_count": 4,
                    }
                }
            }
        )
    )

    metrics = provider.get_post_metrics("at", "999")

    assert (metrics.impressions, metrics.likes, metrics.comments, metrics.shares, metrics.saves) == (500, 10, 2, 4, 4)
    assert metrics.engagements == 20
