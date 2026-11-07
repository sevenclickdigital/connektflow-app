"""Tests for GoogleBusinessProvider publishing: local posts and profile photos."""

from unittest.mock import MagicMock, call

import pytest

from providers.exceptions import PublishError
from providers.google_business import ACCOUNTS_API, BUSINESS_INFO_API, POSTS_API, GoogleBusinessProvider
from providers.types import PostType, PublishContent


def _resp(payload: dict) -> MagicMock:
    return MagicMock(json=MagicMock(return_value=payload))


def _provider(location_id="locations/11"):
    return GoogleBusinessProvider({"client_id": "id", "client_secret": "secret", "location_id": location_id})


def test_a_post_goes_to_the_v4_path_under_the_owning_account():
    """v4 only routes ``accounts/{a}/locations/{l}``; the stored ``locations/{l}`` alone does not."""
    provider = _provider()
    provider._request = MagicMock(
        side_effect=[
            _resp({"accounts": [{"name": "accounts/1"}]}),
            _resp({"name": "accounts/1/locations/11/localPosts/9", "searchUrl": "https://g.co/p"}),
        ]
    )

    result = provider.publish_post("tok", PublishContent(text="Open today", post_type=PostType.TEXT))

    assert result.platform_post_id == "accounts/1/locations/11/localPosts/9"
    assert provider._request.call_args_list[1].args[:2] == ("POST", f"{POSTS_API}/accounts/1/locations/11/localPosts")


def test_with_several_accounts_the_owner_of_the_location_is_found():
    provider = _provider()
    provider._request = MagicMock(
        side_effect=[
            _resp({"accounts": [{"name": "accounts/1"}, {"name": "accounts/2"}]}),
            _resp({"locations": [{"name": "locations/99"}]}),
            _resp({"locations": [{"name": "locations/11"}]}),
            _resp({"name": "accounts/2/locations/11/localPosts/1"}),
        ]
    )

    provider.publish_post("tok", PublishContent(text="hi", post_type=PostType.TEXT))

    assert provider._request.call_args_list[0].args[:2] == ("GET", f"{ACCOUNTS_API}/accounts")
    assert provider._request.call_args_list[1].args[:2] == ("GET", f"{BUSINESS_INFO_API}/accounts/1/locations")
    assert provider._request.call_args_list[3].args[:2] == ("POST", f"{POSTS_API}/accounts/2/locations/11/localPosts")


def test_a_location_no_account_owns_fails_without_retrying():
    provider = _provider()
    provider._request = MagicMock(
        side_effect=[
            _resp({"accounts": [{"name": "accounts/1"}, {"name": "accounts/2"}]}),
            _resp({"locations": []}),
            _resp({"locations": []}),
        ]
    )

    with pytest.raises(PublishError) as exc:
        provider.publish_post("tok", PublishContent(text="hi", post_type=PostType.TEXT))

    assert exc.value.retryable is False


def test_a_profile_photo_is_added_to_the_gallery_with_its_category():
    provider = _provider("accounts/1/locations/11")
    provider._request = MagicMock(
        return_value=_resp({"name": "accounts/1/locations/11/media/abc", "googleUrl": "https://lh3.example/abc"})
    )

    result = provider.publish_post(
        "tok",
        PublishContent(
            text="Our new storefront",
            media_urls=["https://cdn.example.com/front.jpg"],
            media_types=["image"],
            post_type=PostType.PROFILE_PHOTO,
            extra={"photo_category": "EXTERIOR"},
        ),
    )

    assert result.platform_post_id == "accounts/1/locations/11/media/abc"
    assert result.url == "https://lh3.example/abc"
    assert provider._request.call_args == call(
        "POST",
        f"{POSTS_API}/accounts/1/locations/11/media",
        access_token="tok",
        json={
            "mediaFormat": "PHOTO",
            "locationAssociation": {"category": "EXTERIOR"},
            "sourceUrl": "https://cdn.example.com/front.jpg",
            "description": "Our new storefront",
        },
    )


def test_an_unknown_photo_category_falls_back_to_additional():
    provider = _provider("accounts/1/locations/11")
    provider._request = MagicMock(return_value=_resp({"name": "accounts/1/locations/11/media/abc"}))

    provider.publish_post(
        "tok",
        PublishContent(
            text="",
            media_urls=["https://cdn.example.com/x.jpg"],
            media_types=["image"],
            post_type=PostType.PROFILE_PHOTO,
            extra={"photo_category": "COVER"},
        ),
    )

    sent = provider._request.call_args.kwargs["json"]
    assert sent["locationAssociation"] == {"category": "ADDITIONAL"}
    assert "description" not in sent


def test_a_profile_photo_refuses_a_video():
    provider = _provider("accounts/1/locations/11")
    provider._request = MagicMock()

    with pytest.raises(PublishError) as exc:
        provider.publish_post(
            "tok",
            PublishContent(
                text="",
                media_urls=["https://cdn.example.com/x.mp4"],
                media_types=["video"],
                post_type=PostType.PROFILE_PHOTO,
            ),
        )

    assert exc.value.retryable is False
    provider._request.assert_not_called()


def test_profile_photo_metrics_report_view_count():
    provider = _provider()
    provider._request = MagicMock(return_value=_resp({"insights": {"viewCount": "42"}}))

    metrics = provider.get_post_metrics("tok", "accounts/1/locations/11/media/abc")

    assert metrics.impressions == 42
