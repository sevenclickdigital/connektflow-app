"""Composer and idea uploads are typed and named by their bytes.

``media_library/`` is served publicly and the Content-Type of a served file
comes from its suffix, so every composer path that stores an upload has to go
through the media library's validator: a file that is not an accepted media
type is refused, and an accepted one is stored under the extension its bytes
justify, whatever the client called it.
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.composer.models import Idea
from apps.composer.tests.test_upload_storage_quota import PNG_BYTES, UploadQuotaTestCase
from apps.media_library.models import MediaAsset

HTML_BYTES = b"<!doctype html><title>x</title><p>not an image</p>"
PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


class ComposerUploadValidationTests(UploadQuotaTestCase):
    def setUp(self):
        super().setUp()
        self._give_headroom()

    def _url(self, name):
        return reverse(f"composer:{name}", kwargs={"workspace_id": self.workspace.id})

    def _posts(self, upload):
        """One request per composer upload path, each sending ``upload()``."""
        return {
            "upload_media": lambda: self.client.post(self._url("upload_media"), {"file": upload()}),
            "thumbnail_upload": lambda: self.client.post(self._url("thumbnail_upload"), {"file": upload()}),
            "idea_upload_media": lambda: self.client.post(self._url("idea_upload_media"), {"file": upload()}),
            "idea_create": lambda: self.client.post(self._url("idea_create"), {"title": "An idea", "media": upload()}),
        }

    def test_a_file_that_is_not_media_is_refused_on_every_path(self):
        def upload():
            return SimpleUploadedFile("page.html", HTML_BYTES, content_type="image/png")

        for name, post in self._posts(upload).items():
            with self.subTest(path=name):
                response = post()
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())
                self.assertEqual(self._asset_count(), 0)
        self.assertFalse(Idea.objects.filter(workspace=self.workspace).exists())

    def test_an_image_named_html_is_stored_as_the_image_it_is(self):
        def upload():
            return SimpleUploadedFile("page.html", PNG_BYTES, content_type="text/html")

        for name, post in self._posts(upload).items():
            with self.subTest(path=name):
                MediaAsset.objects.filter(workspace=self.workspace).delete()
                response = post()
                self.assertIn(response.status_code, (200, 201, 204))
                asset = MediaAsset.objects.get(workspace=self.workspace)
                self.assertTrue(asset.file.name.endswith(".png"), asset.file.name)
                self.assertEqual(asset.mime_type, "image/png")
                self.assertEqual(asset.media_type, MediaAsset.MediaType.IMAGE)
                self.assertEqual(asset.filename, "page.html")

    def test_the_thumbnail_path_takes_images_only(self):
        response = self.client.post(
            self._url("thumbnail_upload"),
            {"file": SimpleUploadedFile("cover.png", PDF_BYTES, content_type="image/png")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._asset_count(), 0)

    def test_editing_an_idea_refuses_a_file_that_is_not_media(self):
        self.client.post(self._url("idea_create"), {"title": "An idea"})
        idea = Idea.objects.get(workspace=self.workspace)

        response = self.client.post(
            reverse("composer:idea_edit", kwargs={"workspace_id": self.workspace.id, "idea_id": idea.id}),
            {"title": "An idea", "media": SimpleUploadedFile("page.html", HTML_BYTES, content_type="image/png")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._asset_count(), 0)
