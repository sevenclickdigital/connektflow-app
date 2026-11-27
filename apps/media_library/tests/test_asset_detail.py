"""The asset detail view, and a file's versions: edits and crops as copies of the original."""

import io
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

TEMP_MEDIA_ROOT = tempfile.mkdtemp(prefix="bb-test-versions-")


def tearDownModule():
    shutil.rmtree(TEMP_MEDIA_ROOT, ignore_errors=True)


def _png(width=400, height=300):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "purple").save(buffer, format="PNG")
    return ContentFile(buffer.getvalue(), name="photo.png")


# Asserts on rendered copy; pin English so it tests the markup, not the catalogue.
@override_settings(LANGUAGE_CODE="en-us")
class AssetDetailTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.asset = self._asset(self.workspace)
        self.client.force_login(self.user)

    def _asset(self, workspace):
        return MediaAsset.objects.create(
            organization=workspace.organization,
            workspace=workspace,
            uploaded_by=self.user,
            file="media_library/tests/coffee.jpg",
            filename="coffee.jpg",
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/jpeg",
            file_size=2048,
            source="unsplash",
            author_name="Brigitte Tohm",
            author_url="https://unsplash.com/@brigittetohm",
        )

    def _url(self, asset, workspace=None):
        workspace = workspace or self.workspace
        return reverse("media_library:asset_detail", kwargs={"workspace_id": workspace.id, "asset_id": asset.id})

    def test_the_library_modal_has_the_file_actions_and_details(self):
        response = self.client.get(self._url(self.asset), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('alt="coffee.jpg"', html)
        self.assertIn("Download", html)
        self.assertIn("Brigitte Tohm", html)
        self.assertIn('@click="detailOpen = false"', html)

    def test_the_full_page_has_no_modal_close_button(self):
        response = self.client.get(self._url(self.asset))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('@click="detailOpen = false"', response.content.decode())

    def test_the_picker_gets_only_the_details(self):
        response = self.client.get(self._url(self.asset) + "?part=info")

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Brigitte Tohm", html)
        self.assertNotIn("Download", html)
        self.assertNotIn("Delete", html)

    def test_another_workspaces_asset_is_not_found(self):
        other = Workspace.objects.create(organization=Organization.objects.create(name="Other Org"), name="Other")
        foreign = self._asset(other)

        response = self.client.get(self._url(foreign) + "?part=info")

        self.assertEqual(response.status_code, 404)


@override_settings(LANGUAGE_CODE="en-us", MEDIA_ROOT=TEMP_MEDIA_ROOT)
class AssetVersionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        self.org = Organization.objects.create(name="Test Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Test Workspace")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.original = MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            uploaded_by=self.user,
            file=_png(),
            filename="photo.png",
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/png",
            width=400,
            height=300,
        )
        self.client.force_login(self.user)

    def _edit(self, asset, **data):
        url = reverse("media_library:asset_edit", kwargs={"workspace_id": self.workspace.id, "asset_id": asset.id})
        return self.client.post(url, data=data)

    def _copy(self, **fields):
        return MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            uploaded_by=self.user,
            file=_png(200, 200),
            filename="photo-200x200.png",
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/png",
            width=200,
            height=200,
            original=self.original,
            **fields,
        )

    def test_an_edit_is_saved_as_a_copy_and_the_original_is_kept(self):
        original_file = self.original.file.name

        response = self._edit(self.original, flip="horizontal", rotate="0")

        self.assertEqual(response.status_code, 302)
        copy = MediaAsset.objects.get(original=self.original)
        self.assertNotEqual(copy.file.name, original_file)
        self.original.refresh_from_db()
        self.assertEqual(self.original.file.name, original_file)
        self.assertFalse(self.original.versions.exists())

    def test_editing_a_copy_adds_a_version_of_the_same_original(self):
        copy = self._copy()

        self._edit(copy, rotate="-90")

        self.assertEqual(MediaAsset.objects.filter(original=self.original).count(), 2)

    def test_an_untouched_rotation_is_not_an_edit(self):
        response = self._edit(self.original, rotate="0")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Nothing was changed.", response.content.decode())
        self.assertFalse(MediaAsset.objects.filter(original=self.original).exists())

    def test_the_grid_shows_one_card_per_original_with_its_version_count(self):
        self._copy()
        self._copy()

        response = self.client.get(reverse("media_library:index", kwargs={"workspace_id": self.workspace.id}))

        html = response.content.decode()
        self.assertEqual(html.count("photo-200x200.png"), 0)
        self.assertIn("3 versions", html)

    def test_the_detail_lists_the_versions_and_marks_the_one_shown(self):
        copy = self._copy()
        url = reverse("media_library:asset_detail", kwargs={"workspace_id": self.workspace.id, "asset_id": copy.id})

        html = self.client.get(url, HTTP_HX_REQUEST="true").content.decode()

        self.assertIn("Versions", html)
        self.assertIn("Original", html)
        self.assertIn(str(self.original.id), html)
        self.assertEqual(html.count('aria-current="true"'), 1)

    def test_the_picker_gets_the_versions_to_switch_between(self):
        copy = self._copy()
        url = reverse(
            "media_library:asset_detail", kwargs={"workspace_id": self.workspace.id, "asset_id": self.original.id}
        )

        html = self.client.get(url + "?part=info").content.decode()

        self.assertIn(f'data-asset-id="{copy.id}"', html)
        self.assertIn("data-version-pick", html)

    def test_a_copy_in_another_workspace_is_not_listed(self):
        other = Workspace.objects.create(organization=self.org, name="Other")
        hidden = self._copy()
        MediaAsset.objects.filter(pk=hidden.pk).update(workspace=other)
        url = reverse(
            "media_library:asset_detail", kwargs={"workspace_id": self.workspace.id, "asset_id": self.original.id}
        )

        html = self.client.get(url, HTTP_HX_REQUEST="true").content.decode()

        self.assertNotIn(str(hidden.id), html)
        self.assertNotIn("Versions", html)

    def test_in_the_picker_a_file_with_versions_opens_them_instead_of_being_picked(self):
        self._copy()
        single = MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            uploaded_by=self.user,
            file=_png(),
            filename="single.png",
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/png",
        )

        html = self.client.get(
            reverse("composer:media_picker", kwargs={"workspace_id": self.workspace.id}), HTTP_HX_REQUEST="true"
        ).content.decode()

        self.assertIn(f"@click=\"openPreview('{self.original.id}', $el.dataset)\"", html)
        self.assertIn(f"@click=\"toggle('{single.id}', $el.dataset)\"", html)
