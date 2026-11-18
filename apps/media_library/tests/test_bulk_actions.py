"""The gallery's bulk bar: moving and deleting the ticked files."""

import json

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset, MediaFolder
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class BulkActionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.other = Workspace.objects.create(organization=self.org, name="Other")
        self.user = self._member("owner@example.com", WorkspaceMembership.WorkspaceRole.OWNER)
        self.folder = MediaFolder.objects.create(organization=self.org, workspace=self.workspace, name="Campaign")
        self.mine = [self._asset(self.workspace, f"mine-{n}.png") for n in range(2)]
        self.foreign = self._asset(self.other, "foreign.png")
        self.client.force_login(self.user)
        self.move_url = reverse("media_library:asset_bulk_move", kwargs={"workspace_id": self.workspace.id})
        self.delete_url = reverse("media_library:asset_bulk_delete", kwargs={"workspace_id": self.workspace.id})

    def _member(self, email, role):
        user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
        OrgMembership.objects.create(user=user, organization=self.org, org_role=OrgMembership.OrgRole.MEMBER)
        WorkspaceMembership.objects.create(user=user, workspace=self.workspace, workspace_role=role)
        return user

    def _asset(self, workspace, name):
        return MediaAsset.objects.create(
            organization=self.org, workspace=workspace, filename=name, file=f"media/{name}", media_type="image"
        )

    def _post(self, url, data):
        return self.client.post(url, data, HTTP_HX_REQUEST="true")

    def test_moves_the_ticked_files_into_the_folder(self):
        response = self._post(self.move_url, {"ids": [str(a.id) for a in self.mine], "folder_id": str(self.folder.id)})

        self.assertEqual(response.status_code, 204)
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["tone"], "success")
        self.assertIn("assets-changed", trigger)
        self.assertEqual(MediaAsset.objects.filter(folder=self.folder).count(), 2)

    def test_no_folder_takes_the_files_out_of_folders(self):
        MediaAsset.objects.filter(pk=self.mine[0].pk).update(folder=self.folder)

        self._post(self.move_url, {"ids": [str(self.mine[0].id)], "folder_id": ""})

        self.mine[0].refresh_from_db()
        self.assertIsNone(self.mine[0].folder_id)

    def test_another_workspaces_file_is_left_alone(self):
        self._post(self.move_url, {"ids": [str(self.foreign.id), "not-a-uuid"], "folder_id": str(self.folder.id)})
        self._post(self.delete_url, {"ids": [str(self.foreign.id)]})

        self.foreign.refresh_from_db()
        self.assertIsNone(self.foreign.folder_id)

    def test_another_workspaces_folder_is_not_found(self):
        foreign_folder = MediaFolder.objects.create(organization=self.org, workspace=self.other, name="Theirs")

        response = self._post(self.move_url, {"ids": [str(self.mine[0].id)], "folder_id": str(foreign_folder.id)})

        self.assertEqual(response.status_code, 404)
        self.mine[0].refresh_from_db()
        self.assertIsNone(self.mine[0].folder_id)

    def test_deletes_the_ticked_files_but_keeps_one_a_scheduled_post_uses(self):
        account = SocialAccount.objects.create(
            workspace=self.workspace, platform="facebook", account_platform_id="fb-1", account_name="Page"
        )
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="Soon")
        PlatformPost.objects.create(post=post, social_account=account, status=PlatformPost.Status.SCHEDULED)
        PostMedia.objects.create(post=post, media_asset=self.mine[0])

        response = self._post(self.delete_url, {"ids": [str(a.id) for a in self.mine]})

        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["tone"], "warn")
        self.assertTrue(MediaAsset.objects.filter(pk=self.mine[0].pk).exists())
        self.assertFalse(MediaAsset.objects.filter(pk=self.mine[1].pk).exists())

    def test_a_viewer_can_neither_move_nor_delete(self):
        viewer = self._member("viewer@example.com", WorkspaceMembership.WorkspaceRole.VIEWER)
        self.client.force_login(viewer)
        ids = [str(a.id) for a in self.mine]

        self.assertEqual(self._post(self.move_url, {"ids": ids, "folder_id": str(self.folder.id)}).status_code, 403)
        self.assertEqual(self._post(self.delete_url, {"ids": ids}).status_code, 403)
        self.assertEqual(MediaAsset.objects.filter(workspace=self.workspace).count(), 2)

    def test_the_gallery_offers_selection_only_to_who_can_act_on_it(self):
        index_url = reverse("media_library:index", kwargs={"workspace_id": self.workspace.id})
        self.assertContains(self.client.get(index_url), "toggleSelected('")

        viewer = self._member("viewer@example.com", WorkspaceMembership.WorkspaceRole.VIEWER)
        self.client.force_login(viewer)
        self.assertNotContains(self.client.get(index_url), "toggleSelected('")
