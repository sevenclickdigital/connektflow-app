"""The asset detail view: the library's modal and the media picker's details."""

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


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
