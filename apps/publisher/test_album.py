"""More than ten photos on Facebook go out as a Page album.

A feed post takes at most ten photos, and Meta offers no way to delete an
album through the API, so a retry must keep adding to the album an earlier
attempt created rather than make a second one.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.publisher.engine import ALBUM_PROGRESS_KEY, album_progress
from providers.exceptions import PublishError
from providers.facebook import FACEBOOK_ALBUM_ID_PREFIX, FacebookProvider
from providers.types import PostType, PublishContent, PublishResult

from .test_media_handling import _attachment, _dispatch_mocks


def _resp(data):
    return MagicMock(json=MagicMock(return_value=data))


def _content(count, *, text="Our week\nMore below", extra=None):
    return PublishContent(
        text=text,
        media_urls=[f"https://cdn.example/{i}.jpg" for i in range(count)],
        media_types=["image"] * count,
        post_type=PostType.IMAGE,
        extra={"page_id": "page-1", **(extra or {})},
    )


class FacebookAlbumTests(SimpleTestCase):
    def test_the_album_is_created_then_filled_in_order(self):
        provider = FacebookProvider({"page_id": "page-1"})
        provider._request = MagicMock(
            side_effect=[_resp({"id": "alb-1"})] + [_resp({"id": f"ph-{i}"}) for i in range(12)]
        )
        saved = []

        result = provider.publish_album(
            "tok", _content(12, extra={"place_id": "pl-1"}), progress={}, save_progress=saved.append
        )

        create, *photos = provider._request.call_args_list
        assert create.args[1].endswith("/page-1/albums")
        assert create.kwargs["json"] == {"name": "Our week", "message": "Our week\nMore below", "place": "pl-1"}
        assert [c.kwargs["json"]["url"] for c in photos] == [f"https://cdn.example/{i}.jpg" for i in range(12)]
        assert all(c.args[1].endswith("/alb-1/photos") for c in photos)
        assert result.platform_post_id == f"{FACEBOOK_ALBUM_ID_PREFIX}alb-1"
        assert saved[-1] == {"album_id": "alb-1", "photo_ids": [f"ph-{i}" for i in range(12)]}

    def test_a_retry_adds_the_missing_photos_to_the_same_album(self):
        provider = FacebookProvider({"page_id": "page-1"})
        provider._request = MagicMock(side_effect=[_resp({"id": "ph-10"}), _resp({"id": "ph-11"})])
        done = [f"ph-{i}" for i in range(10)]

        provider.publish_album(
            "tok", _content(12), progress={"album_id": "alb-1", "photo_ids": done}, save_progress=lambda _p: None
        )

        assert [c.kwargs["json"]["url"] for c in provider._request.call_args_list] == [
            "https://cdn.example/10.jpg",
            "https://cdn.example/11.jpg",
        ]

    def test_a_video_is_refused_before_anything_is_created(self):
        provider = FacebookProvider({"page_id": "page-1"})
        provider._request = MagicMock()
        content = _content(11)
        content.media_types[3] = "video"

        with self.assertRaises(PublishError):
            provider.publish_album("tok", content, progress={}, save_progress=lambda _p: None)
        provider._request.assert_not_called()

    def test_a_first_comment_goes_to_the_album_itself(self):
        provider = FacebookProvider({"page_id": "page-1"})

        assert provider._comment_target_ids(f"{FACEBOOK_ALBUM_ID_PREFIX}alb-1") == ["alb-1"]


@patch("apps.publisher.engine._resolve_publish_credentials", return_value={})
@patch("apps.publisher.engine.get_provider")
class AlbumRoutingTests(SimpleTestCase):
    def _mocks(self, count, *, platform_extra=None):
        engine, platform_post, provider = _dispatch_mocks(
            "facebook",
            needs_local_media=False,
            attachments=[_attachment(f"a{i}", filename=f"{i}.jpg") for i in range(count)],
            supported=(PostType.IMAGE,),
        )
        platform_post.id = "row-1"
        platform_post.format = "default"
        platform_post.platform_extra = platform_extra or {}
        provider.album_threshold = 10
        provider.publish_album.return_value = PublishResult(platform_post_id="album_alb-1", url=None, extra={})
        return engine, platform_post, provider

    def test_eleven_photos_go_to_the_album_flow_with_saved_progress(self, get_provider, _creds):
        engine, platform_post, provider = self._mocks(11)
        get_provider.return_value = provider

        engine._dispatch_to_provider(platform_post)

        provider.publish_post.assert_not_called()
        kwargs = provider.publish_album.call_args.kwargs
        assert kwargs["progress"] == {}
        kwargs["save_progress"]({"album_id": "alb-1", "photo_ids": ["ph-0"]})
        assert album_progress(platform_post) == {"album_id": "alb-1", "photo_ids": ["ph-0"]}

    def test_ten_photos_stay_one_post(self, get_provider, _creds):
        engine, platform_post, provider = self._mocks(10)
        get_provider.return_value = provider

        engine._dispatch_to_provider(platform_post)

        provider.publish_album.assert_not_called()
        provider.publish_post.assert_called_once()

    def test_a_clone_does_not_reuse_the_originals_album(self, get_provider, _creds):
        copied = {ALBUM_PROGRESS_KEY: {"row": "original-row", "album_id": "alb-0", "photo_ids": ["ph-0"]}}
        engine, platform_post, provider = self._mocks(11, platform_extra=copied)
        get_provider.return_value = provider

        engine._dispatch_to_provider(platform_post)

        assert provider.publish_album.call_args.kwargs["progress"] == {}


class AlbumAnalyticsTests(SimpleTestCase):
    def test_an_album_is_left_out_of_post_insights(self):
        from types import SimpleNamespace

        from apps.analytics.tasks import _has_unusable_platform_post_id

        album = SimpleNamespace(id="row-1", platform_post_id=f"{FACEBOOK_ALBUM_ID_PREFIX}alb-1")
        post = SimpleNamespace(id="row-2", platform_post_id="123456")

        assert _has_unusable_platform_post_id(album, "facebook")
        assert not _has_unusable_platform_post_id(post, "facebook")
