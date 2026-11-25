"""Trimming an attached video for one format of an account (Story, Short)."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from background_task.models import Task
from django.conf import settings
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import translation

from apps.composer.models import PlatformPost, PostMedia
from apps.composer.services import apply_format_media, require_publishable
from apps.composer.tests.test_account_scope import AccountScopeTestsBase
from apps.media_library.models import MediaAsset
from apps.media_library.services import processing_error_message
from apps.media_library.tasks import trim_video_copy
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _tiny_video(seconds=3):
    """A real ``seconds``-long 160x284 MP4 with audio, made by ffmpeg."""
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x284:rate=10:duration={seconds}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path,
            ],
            capture_output=True,
            check=True,
        )  # fmt: skip
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


class TrimFormatMediaTestsBase(AccountScopeTestsBase):
    video_bytes = b"not really a video"
    duration = 69.2

    def setUp(self):
        super().setUp()
        self.post.platform_posts.update(status="draft")
        self.asset = MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            uploaded_by=self.user,
            file=ContentFile(self.video_bytes, name="vidro.mp4"),
            filename="vidro.mp4",
            media_type=MediaAsset.MediaType.VIDEO,
            mime_type="video/mp4",
            duration=self.duration,
            file_size=104 * 1024 * 1024,
            width=1080,
            height=1920,
        )
        self.attachment = PostMedia.objects.create(post=self.post, media_asset=self.asset, position=1)
        self.instagram = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="instagram",
            account_platform_id="ig-trim",
            account_name="Instagram",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.feed = PlatformPost.objects.create(post=self.post, social_account=self.instagram)
        self.story = PlatformPost.objects.create(post=self.post, social_account=self.instagram, format="story")
        self.url = reverse(
            "composer:trim_format_media", kwargs={"workspace_id": self.workspace.id, "post_id": self.post.id}
        )

    def _trim(self, start=0, end=60, fmt="story", **extra):
        return self.client.post(
            self.url,
            data={
                "asset_id": str(self.asset.id),
                "target": f"{self.instagram.id}:{fmt}",
                "start": start,
                "end": end,
                **extra,
            },
        )


class TrimFormatMediaViewTests(TrimFormatMediaTestsBase):
    def test_starts_an_empty_copy_and_schedules_the_trim(self):
        response = self._trim(start=2, end=62)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        copy = MediaAsset.objects.get(pk=payload["copy"])
        self.assertEqual(copy.derived_from_id, self.asset.pk)
        self.assertEqual(copy.processing_status, MediaAsset.ProcessingStatus.PROCESSING)
        self.assertFalse(copy.file)
        self.assertEqual(payload["source"], str(self.asset.id))
        self.assertEqual(payload["target"], f"{self.instagram.id}:story")
        task = Task.objects.get(task_name="apps.media_library.tasks.trim_video_copy")
        args, kwargs = json.loads(task.task_params)
        self.assertEqual(args, [str(copy.id), 2.0, 62.0])
        # The Story's limits come from the server, not the page.
        self.assertEqual((kwargs["max_seconds"], kwargs["max_bytes"]), (60, 100 * 1024 * 1024))
        self.assertEqual(kwargs["platform_post_ids"], [str(self.story.pk)])
        # A post format's copy is brought down to what a phone shows.
        self.assertTrue(kwargs["fit_for_phone"])
        # Nothing is swapped in before the copy is ready.
        self.story.refresh_from_db()
        self.assertEqual(self.story.platform_specific_media or [], [])

    def test_a_stretch_longer_than_the_format_takes_is_refused(self):
        response = self._trim(start=0, end=61, max_seconds=999)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertFalse(MediaAsset.objects.filter(derived_from=self.asset).exists())

    def test_a_stretch_outside_the_video_or_unreadable_is_refused(self):
        for start, end in ((10, 5), (0, 0.5), ("abc", 10), (60, 75), ("nan", 10)):
            with self.subTest(start=start, end=end):
                self.assertEqual(self._trim(start=start, end=end).status_code, 400)
        self.assertFalse(MediaAsset.objects.filter(derived_from=self.asset).exists())

    def test_a_format_the_post_does_not_have_is_not_found(self):
        self.assertEqual(self._trim(fmt="reel").status_code, 404)

    def test_another_workspace_cannot_trim_this_video(self):
        other = Workspace.objects.create(organization=self.org, name="Other")
        url = reverse("composer:trim_format_media", kwargs={"workspace_id": other.id, "post_id": self.post.id})

        response = self.client.post(
            url,
            data={"asset_id": str(self.asset.id), "target": f"{self.instagram.id}:story", "start": 0, "end": 60},
        )

        self.assertIn(response.status_code, (403, 404))
        self.assertFalse(MediaAsset.objects.filter(derived_from=self.asset).exists())

    def test_a_viewer_cannot_trim(self):
        WorkspaceMembership.objects.filter(user=self.user, workspace=self.workspace).update(
            workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )

        self.assertEqual(self._trim().status_code, 403)

    def test_a_published_format_is_not_trimmed(self):
        self.story.status = PlatformPost.Status.PUBLISHED
        self.story.save(update_fields=["status"])

        self.assertIn(self._trim().status_code, (400, 404))


class TrimVideoCopyTaskTests(TrimFormatMediaTestsBase):
    def _start(self, **limits):
        from apps.media_library.services import start_video_trim

        return start_video_trim(
            self.asset,
            organization=self.org,
            workspace=self.workspace,
            start=0,
            end=1.5,
            uploaded_by=self.user,
            platform_post_ids=[self.story.pk],
            **limits,
        )

    def test_a_failure_marks_the_copy_failed_and_leaves_everything_else_alone(self):
        copy = self._start()
        source_name = self.asset.file.name
        with (
            mock.patch("apps.media_library.tasks.extract_video_metadata", return_value={"duration_seconds": 69.2}),
            mock.patch("apps.media_library.tasks.trim_video", side_effect=RuntimeError("boom")),
        ):
            trim_video_copy.now(str(copy.id), 0, 1.5, platform_post_ids=[str(self.story.pk)])

        copy.refresh_from_db()
        self.assertEqual(copy.processing_status, MediaAsset.ProcessingStatus.FAILED)
        self.assertEqual(copy.processing_error, "trim_failed")
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.file.name, source_name)
        self.story.refresh_from_db()
        self.assertEqual(self.story.platform_specific_media or [], [])
        self.assertFalse([n for n in os.listdir(tempfile.gettempdir()) if n.startswith("connektflow_trim_")])

    def test_the_failure_reason_is_what_the_status_poll_shows(self):
        copy = self._start()
        MediaAsset.objects.filter(pk=copy.pk).update(
            processing_status=MediaAsset.ProcessingStatus.FAILED, processing_error="trim_too_large"
        )
        url = reverse(
            "media_library:processing_status", kwargs={"workspace_id": self.workspace.id, "asset_id": copy.id}
        )

        payload = self.client.get(url).json()

        self.assertEqual(payload["status"], "failed")
        with translation.override(settings.LANGUAGE_CODE):
            self.assertEqual(payload["error"], processing_error_message("trim_too_large"))

    def test_the_status_poll_reports_progress_while_the_trim_runs(self):
        copy = self._start()
        MediaAsset.objects.filter(pk=copy.pk).update(processing_progress=42)
        url = reverse(
            "media_library:processing_status", kwargs={"workspace_id": self.workspace.id, "asset_id": copy.id}
        )

        payload = self.client.get(url).json()

        self.assertEqual((payload["status"], payload["progress"]), ("processing", 42))

    def test_a_trim_whose_worker_died_or_never_ran_is_marked_failed(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.media_library.tasks import fail_stalled_video_trims

        long_ago = timezone.now() - timedelta(hours=2)
        running = self._start()  # job taken, then silent for two hours
        Task.objects.filter(task_params__contains=str(running.pk)).update(locked_by="media-1", locked_at=long_ago)
        MediaAsset.objects.filter(pk=running.pk).update(updated_at=long_ago)
        queued = self._start()  # never taken in two hours
        MediaAsset.objects.filter(pk=queued.pk).update(created_at=long_ago)
        lost = self._start()  # its job is gone
        Task.objects.filter(task_params__contains=str(lost.pk)).delete()
        MediaAsset.objects.filter(pk=lost.pk).update(created_at=long_ago)
        alive = self._start()  # taken a moment ago, still reporting
        Task.objects.filter(task_params__contains=str(alive.pk)).update(locked_by="media-1", locked_at=timezone.now())
        waiting = self._start()  # just queued

        fail_stalled_video_trims.now()

        status = dict(MediaAsset.objects.filter(derived_from=self.asset).values_list("pk", "processing_error"))
        self.assertEqual(status[running.pk], "trim_stalled")
        self.assertEqual(status[queued.pk], "trim_stalled")
        self.assertEqual(status[lost.pk], "trim_stalled")
        self.assertEqual(status[alive.pk], "")
        self.assertEqual(status[waiting.pk], "")

    def test_a_copy_already_settled_is_not_trimmed_again(self):
        copy = self._start()
        MediaAsset.objects.filter(pk=copy.pk).update(processing_status=MediaAsset.ProcessingStatus.COMPLETED)
        with mock.patch("apps.media_library.tasks.trim_video") as trim:
            trim_video_copy.now(str(copy.id), 0, 1.5)

        trim.assert_not_called()


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg is not installed")
class TrimVideoCopyWithFfmpegTests(TrimFormatMediaTestsBase):
    video_bytes = _tiny_video(3) if HAS_FFMPEG else b""
    duration = 3.0

    def test_the_story_publishes_the_trimmed_copy_and_the_feed_the_original(self):
        from apps.media_library.services import extract_video_metadata, start_video_trim

        copy = start_video_trim(
            self.asset,
            organization=self.org,
            workspace=self.workspace,
            start=0.5,
            end=2.0,
            uploaded_by=self.user,
            max_seconds=60,
            max_bytes=100 * 1024 * 1024,
            platform_post_ids=[self.story.pk],
        )
        trim_video_copy.now(
            str(copy.id), 0.5, 2.0, max_seconds=60, max_bytes=100 * 1024 * 1024, platform_post_ids=[str(self.story.pk)]
        )

        copy.refresh_from_db()
        self.assertEqual(copy.processing_status, MediaAsset.ProcessingStatus.COMPLETED)
        self.assertAlmostEqual(copy.duration, 1.5, delta=0.15)
        self.assertEqual(copy.processing_progress, 100)
        self.assertEqual((copy.width, copy.height), (160, 284))
        self.assertGreater(copy.file_size, 0)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as out:
            out.write(copy.file.read())
            out.flush()
            self.assertAlmostEqual(extract_video_metadata(out.name)["duration_seconds"], 1.5, delta=0.15)

        self.story.refresh_from_db()
        self.feed.refresh_from_db()

        def attachments():
            return list(self.post.media_attachments.select_related("media_asset"))

        self.assertEqual(apply_format_media(self.story, attachments())[0].media_asset_id, copy.pk)
        self.assertEqual(apply_format_media(self.feed, attachments())[0].media_asset_id, self.asset.pk)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.duration, 3.0)

    def test_ffmpeg_reports_how_far_the_cut_has_got(self):
        from apps.media_library.services import trim_video

        reported = []
        with tempfile.NamedTemporaryFile(suffix=".mp4") as src, tempfile.NamedTemporaryFile(suffix=".mp4") as out:
            src.write(self.video_bytes)
            src.flush()
            trim_video(src.name, out.name, 0, 2.5, on_progress=reported.append)

        self.assertTrue(reported)
        self.assertEqual(reported, sorted(reported))
        self.assertGreater(reported[-1], 0.9)
        self.assertLessEqual(reported[-1], 1.0)

    def test_fit_for_phone_brings_a_large_video_down_to_1080_and_30_fps(self):
        from apps.media_library.services import trim_video

        def probe(path):
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height,r_frame_rate", "-of", "csv=p=0", path],
                capture_output=True, text=True, check=True,
            )  # fmt: skip
            return out.stdout.strip()

        with tempfile.NamedTemporaryFile(suffix=".mp4") as src, tempfile.NamedTemporaryFile(suffix=".mp4") as out:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                 "testsrc=size=1200x2000:rate=60:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", src.name],
                check=True,
            )  # fmt: skip
            trim_video(src.name, out.name, 0, 1, fit_for_phone=True)
            self.assertEqual(probe(out.name), "1080,1800,30/1")
            trim_video(src.name, out.name, 0, 1)
            self.assertEqual(probe(out.name), "1200,2000,60/1")

    def test_a_result_over_the_size_limit_fails_with_that_reason(self):
        from apps.media_library.services import start_video_trim

        copy = start_video_trim(
            self.asset, organization=self.org, workspace=self.workspace, start=0, end=2, uploaded_by=self.user
        )
        trim_video_copy.now(str(copy.id), 0, 2, max_bytes=10)

        copy.refresh_from_db()
        self.assertEqual(copy.processing_status, MediaAsset.ProcessingStatus.FAILED)
        self.assertEqual(copy.processing_error, "trim_too_large")


class FormatVideoBlockerTests(TrimFormatMediaTestsBase):
    """The Story is judged by the video it publishes: its trimmed copy when it has one."""

    def test_the_full_video_blocks_the_story_and_its_trimmed_copy_does_not(self):
        with self.assertRaises(ValueError):
            require_publishable(self.story)

        copy = MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            file=ContentFile(b"trimmed", name="vidro-0-60s.mp4"),
            filename="vidro-0-60s.mp4",
            media_type=MediaAsset.MediaType.VIDEO,
            duration=59.9,
            file_size=80 * 1024 * 1024,
            width=1080,
            height=1920,
            derived_from=self.asset,
        )
        self.story.platform_specific_media = [str(copy.pk)]
        self.story.save(update_fields=["platform_specific_media"])

        require_publishable(self.story)
