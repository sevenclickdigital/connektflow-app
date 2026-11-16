"""The library page and its folder endpoints: header, current folder, folder errors as toasts."""

import json

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.media_library.models import MediaFolder
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


class LibraryFolderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        self.folder = MediaFolder.objects.create(organization=self.org, workspace=self.workspace, name="Campanha's")
        self.client.force_login(self.user)
        self.index_url = reverse("media_library:index", kwargs={"workspace_id": self.workspace.id})
        self.create_url = reverse("media_library:folder_create", kwargs={"workspace_id": self.workspace.id})

    def test_page_has_header_and_names_the_open_folder(self):
        response = self.client.get(self.index_url, {"folder": str(self.folder.id)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="page-header"')
        self.assertContains(response, "ml-new-folder")
        self.assertContains(response, "Campanha&#x27;s")

    def test_folder_name_is_escaped_inside_the_alpine_expression(self):
        response = self.client.get(self.index_url)
        self.assertContains(response, "folderDeleteName = 'Campanha\\u0027s'")

    def test_open_folder_of_another_workspace_is_not_named(self):
        other_ws = Workspace.objects.create(organization=self.org, name="Other")
        foreign = MediaFolder.objects.create(organization=self.org, workspace=other_ws, name="Secret folder")
        response = self.client.get(self.index_url, {"folder": str(foreign.id)})
        self.assertNotContains(response, "Secret folder")

    def test_duplicate_folder_shows_an_error_toast(self):
        response = self.client.post(self.create_url, {"name": "Campanha's"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        toast = json.loads(response["HX-Trigger"])["showToast"]
        self.assertEqual(toast["tone"], "error")
        self.assertEqual(MediaFolder.objects.filter(workspace=self.workspace).count(), 1)

    def test_blank_folder_name_shows_an_error_toast(self):
        response = self.client.post(self.create_url, {"name": "  "}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(json.loads(response["HX-Trigger"])["showToast"]["tone"], "error")

    def test_new_folder_is_listed(self):
        response = self.client.post(self.create_url, {"name": "Lançamentos"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lançamentos")
