"""Deleting a workspace or an organization removes its stored files.

The cascade only removes rows, and the orphan sweep starts from rows, so a
file left behind by a deleted tenant would stay at its public URL for good.
"""

import io
import json
from unittest import mock

import pytest
from background_task.models import Task
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User
from apps.media_library import services
from apps.media_library.models import MediaAsset
from apps.media_library.tasks import remove_stored_files
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

REMOVAL_TASK = "apps.media_library.tasks.remove_stored_files"


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 200, 10)).save(buf, format="PNG")
    return buf.getvalue()


def _upload(name):
    return SimpleUploadedFile(name, _png_bytes(), content_type="image/png")


def _queued_names():
    """Every file name handed to the removal task so far."""
    names = []
    for task in Task.objects.filter(task_name=REMOVAL_TASK):
        args, _kwargs = json.loads(task.task_params)
        names.extend(args[0])
    return names


class TenantFileRemovalTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com", password="pw-123456789", name="Owner", tos_accepted_at=timezone.now()
        )
        self.org = Organization.objects.create(name="Org")
        OrgMembership.objects.create(user=self.owner, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        self.kept = Workspace.objects.create(name="Kept", organization=self.org)
        self.target = Workspace.objects.create(name="Target", organization=self.org)
        for workspace in (self.kept, self.target):
            WorkspaceMembership.objects.create(
                user=self.owner, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
            )

        self.asset = services.create_asset(self.org, self.target, _upload("target.png"), self.owner)
        self.asset.thumbnail.save("thumb_target.jpg", ContentFile(_png_bytes()), save=True)
        self.version = services.create_version(self.asset, self.asset.file, "edit", self.owner)
        self.version.file.save("target_v2.png", ContentFile(_png_bytes()), save=True)
        # create_version rebinds the asset's FieldFile to the version, so the
        # asset in hand no longer names its own file.
        self.asset = MediaAsset.objects.get(pk=self.asset.pk)
        self.target.icon.save("icon.png", ContentFile(_png_bytes()), save=True)
        self.kept_asset = services.create_asset(self.org, self.kept, _upload("kept.png"), self.owner)
        self.shared_asset = services.create_asset(self.org, None, _upload("shared.png"), self.owner)

        self.target_files = {
            self.asset.file.name,
            self.asset.thumbnail.name,
            self.version.file.name,
            self.target.icon.name,
        }
        self.addCleanup(self._remove_test_files)

    def _remove_test_files(self):
        for name in self.target_files | {self.kept_asset.file.name, self.shared_asset.file.name}:
            default_storage.delete(name)

    def _delete_target_workspace(self):
        self.client.force_login(self.owner)
        return self.client.post(
            reverse("workspaces:settings", kwargs={"workspace_id": self.target.id}), {"action": "delete_workspace"}
        )

    def test_deleting_a_workspace_removes_its_files_and_only_its_files(self):
        response = self._delete_target_workspace()

        assert response.status_code == 302
        assert not Workspace.objects.filter(pk=self.target.pk).exists()
        assert not MediaAsset.objects.filter(pk=self.asset.pk).exists()
        assert set(_queued_names()) == self.target_files

        remove_stored_files.now(_queued_names())

        for name in self.target_files:
            assert not default_storage.exists(name), name
        assert default_storage.exists(self.kept_asset.file.name)
        assert default_storage.exists(self.shared_asset.file.name)

    def test_a_refused_deletion_queues_nothing(self):
        self.kept.is_archived = True
        self.kept.save(update_fields=["is_archived"])

        self._delete_target_workspace()

        assert Workspace.objects.filter(pk=self.target.pk).exists()
        assert _queued_names() == []
        assert default_storage.exists(self.asset.file.name)

    def test_deleting_an_organization_removes_workspace_and_shared_files(self):
        self.org.hard_delete()

        assert not Organization.objects.filter(pk=self.org.pk).exists()
        expected = self.target_files | {self.kept_asset.file.name, self.shared_asset.file.name}
        assert set(_queued_names()) == expected

        remove_stored_files.now(_queued_names())

        for name in expected:
            assert not default_storage.exists(name), name

    def test_a_file_a_row_still_points_at_is_kept(self):
        result = services.delete_stored_files([self.kept_asset.file.name])

        assert result == {"deleted": 0, "kept": 1, "errors": 0}
        assert default_storage.exists(self.kept_asset.file.name)

    def test_a_storage_failure_is_logged_and_does_not_stop_the_rest(self):
        self._delete_target_workspace()
        names = _queued_names()
        broken = self.asset.file.name

        def delete(name):
            if name == broken:
                raise OSError("storage is down")
            default_storage.delete(name)

        with (
            mock.patch("apps.media_library.storage.delete_object", side_effect=delete),
            self.assertLogs("apps.media_library.services", level="ERROR") as logs,
            pytest.raises(RuntimeError),
        ):
            remove_stored_files.now(names)

        assert broken in logs.output[0]
        assert not Workspace.objects.filter(pk=self.target.pk).exists()
        assert default_storage.exists(broken)
        for name in self.target_files - {broken}:
            assert not default_storage.exists(name), name
