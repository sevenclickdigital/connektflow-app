"""Folders in the composer's media picker.

The picker uploads into the folder it has open and creates folders through
media_library.services.create_folder, which must refuse a duplicate sibling
name even at the top level (the database constraint cannot: PostgreSQL never
treats two NULL parents as equal).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import override_settings
from django.urls import reverse

from apps.media_library.models import MediaAsset, MediaFolder
from apps.media_library.services import create_folder
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

from .test_upload_storage_quota import TEMP_MEDIA_ROOT, UploadQuotaTestCase, _upload


@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class PickerUploadIntoFolderTests(UploadQuotaTestCase):
    def _url(self):
        return reverse("composer:upload_media", kwargs={"workspace_id": self.workspace.id})

    def test_the_upload_lands_in_the_open_folder(self):
        folder = create_folder(self.org, self.workspace, "Campaign")

        response = self.client.post(self._url(), {"file": _upload(), "folder_id": str(folder.id)})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(MediaAsset.objects.get(workspace=self.workspace).folder, folder)

    def test_a_folder_of_another_workspace_is_refused(self):
        other_org = Organization.objects.create(name="Other Org")
        other_workspace = Workspace.objects.create(organization=other_org, name="Other")
        foreign = create_folder(other_org, other_workspace, "Theirs")

        response = self.client.post(self._url(), {"file": _upload(), "folder_id": str(foreign.id)})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(MediaAsset.objects.exists())

    def test_a_malformed_folder_id_is_a_404(self):
        response = self.client.post(self._url(), {"file": _upload(), "folder_id": "not-a-uuid"})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(MediaAsset.objects.exists())


@override_settings(MEDIA_ROOT=TEMP_MEDIA_ROOT)
class CreateFolderNameTests(UploadQuotaTestCase):
    def test_a_duplicate_top_level_name_is_refused(self):
        create_folder(self.org, self.workspace, "Campaign")

        with self.assertRaises(ValidationError):
            create_folder(self.org, self.workspace, "Campaign")
        self.assertEqual(MediaFolder.objects.filter(workspace=self.workspace).count(), 1)

    def test_the_same_name_under_different_parents_is_allowed(self):
        first = create_folder(self.org, self.workspace, "2026")
        second = create_folder(self.org, self.workspace, "2027")

        create_folder(self.org, self.workspace, "Stories", parent_folder=first)
        create_folder(self.org, self.workspace, "Stories", parent_folder=second)

        self.assertEqual(MediaFolder.objects.filter(name="Stories").count(), 2)

    def test_the_picker_create_endpoint_refuses_the_duplicate(self):
        url = reverse("media_library:folder_create", kwargs={"workspace_id": self.workspace.id})
        self.assertEqual(self.client.post(url, {"name": "Campaign"}).status_code, 200)

        response = self.client.post(url, {"name": "Campaign"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MediaFolder.objects.filter(workspace=self.workspace).count(), 1)
