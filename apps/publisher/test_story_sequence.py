"""A Story row with several media goes out as one Story per item.

Meta's Story APIs take one photo or video per call, so the engine publishes the
items in order and records each live Story on the row. A retry must resume
after the last one that went out, never post the earlier ones again.
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from apps.publisher.engine import STORY_PROGRESS_KEY, published_stories, with_story_progress
from providers.exceptions import PublishError
from providers.types import PostType, PublishResult

from .test_media_handling import _attachment, _dispatch_mocks


def _story_mocks(count, *, platform_extra=None):
    attachments = [_attachment(f"a{i}", filename=f"{i}.jpg") for i in range(count)]
    engine, platform_post, provider = _dispatch_mocks(
        "instagram", needs_local_media=False, attachments=attachments, supported=(PostType.STORY,)
    )
    platform_post.id = "row-1"
    platform_post.format = "story"
    platform_post.platform_extra = platform_extra or {}
    provider.publish_post.side_effect = [
        PublishResult(platform_post_id=f"story-{i}", url=f"https://ig/{i}", extra={}) for i in range(count)
    ]
    return engine, platform_post, provider


@patch("apps.publisher.engine._resolve_publish_credentials", return_value={})
@patch("apps.publisher.engine.get_provider")
class StorySequenceTests(SimpleTestCase):
    def test_each_item_goes_out_as_its_own_story(self, get_provider, _creds):
        engine, platform_post, provider = _story_mocks(3)
        get_provider.return_value = provider

        result = engine._dispatch_to_provider(platform_post)

        calls = [call.args[1] for call in provider.publish_post.call_args_list]
        self.assertEqual([c.media_urls for c in calls], [[f"https://cdn.example/{i}.jpg"] for i in range(3)])
        self.assertTrue(all(c.post_type is PostType.STORY for c in calls))
        self.assertEqual(len({c.idempotency_key for c in calls}), 3)
        self.assertEqual(result["platform_post_id"], "story-0")
        self.assertEqual(result["response"]["story_ids"], ["story-0", "story-1", "story-2"])
        self.assertEqual(published_stories(platform_post), ["story-0", "story-1", "story-2"])

    def test_a_retry_resumes_after_the_stories_already_live(self, get_provider, _creds):
        progress = {STORY_PROGRESS_KEY: {"row": "row-1", "ids": ["story-0"], "total": 3}}
        engine, platform_post, provider = _story_mocks(3, platform_extra=progress)
        provider.publish_post.side_effect = [
            PublishResult(platform_post_id="story-1", url=None, extra={}),
            PublishResult(platform_post_id="story-2", url=None, extra={}),
        ]
        get_provider.return_value = provider

        result = engine._dispatch_to_provider(platform_post)

        urls = [call.args[1].media_urls for call in provider.publish_post.call_args_list]
        self.assertEqual(urls, [["https://cdn.example/1.jpg"], ["https://cdn.example/2.jpg"]])
        self.assertEqual(result["response"]["story_ids"], ["story-0", "story-1", "story-2"])

    def test_a_clone_does_not_take_the_originals_stories_as_its_own(self, get_provider, _creds):
        copied = {STORY_PROGRESS_KEY: {"row": "original-row", "ids": ["story-0", "story-1"], "total": 2}}
        engine, platform_post, provider = _story_mocks(2, platform_extra=copied)
        get_provider.return_value = provider

        engine._dispatch_to_provider(platform_post)

        self.assertEqual(provider.publish_post.call_count, 2)

    def test_a_failure_part_way_keeps_and_reports_the_stories_that_went_out(self, get_provider, _creds):
        engine, platform_post, provider = _story_mocks(3)
        provider.publish_post.side_effect = [
            PublishResult(platform_post_id="story-0", url=None, extra={}),
            PublishError("boom", platform="instagram"),
        ]
        get_provider.return_value = provider

        with self.assertRaises(PublishError):
            engine._dispatch_to_provider(platform_post)

        self.assertEqual(published_stories(platform_post), ["story-0"])
        self.assertEqual(
            with_story_progress(platform_post, "Publishing failed. Please try again."),
            "1 of 3 stories already went out. Publishing failed. Please try again.",
        )

    def test_a_single_story_is_one_untouched_call(self, get_provider, _creds):
        engine, platform_post, provider = _story_mocks(1)
        get_provider.return_value = provider

        engine._dispatch_to_provider(platform_post)

        content = provider.publish_post.call_args.args[1]
        self.assertEqual(content.idempotency_key, "brightbean-row-1")
        self.assertEqual(published_stories(platform_post), [])
