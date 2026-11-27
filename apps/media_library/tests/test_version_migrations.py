"""``0007``/``0008`` over a library that already has crops and edit versions.

Crops made before ``MediaAsset.original`` existed must be grouped under the
first file of their chain, and an edit that lived only as a
``MediaAssetVersion`` must become a copy that owns its file, while a version
whose edit was never written stays as it is.
"""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.media_library.models import MediaAsset, MediaAssetVersion
from apps.workspaces.models import Workspace

APP = "media_library"
BEFORE = "0006_asset_author"


def _migrate(target):
    MigrationExecutor(connection).migrate([(APP, target)])


def _head():
    return MigrationExecutor(connection).loader.graph.leaf_nodes(APP)[0][1]


def _models_at(target):
    state_apps = MigrationExecutor(connection).loader.project_state((APP, target)).apps
    return state_apps.get_model(APP, "MediaAsset"), state_apps.get_model(APP, "MediaAssetVersion")


@pytest.fixture
def restore_migrations():
    yield
    _migrate(_head())


@pytest.mark.django_db(transaction=True)
def test_existing_crops_and_versions_become_versions_of_their_original(user, organization, restore_migrations):
    workspace = Workspace.objects.create(organization=organization, name="Studio")
    _migrate(BEFORE)
    old_asset, old_version = _models_at(BEFORE)

    def asset(name, **fields):
        return old_asset.objects.create(
            organization_id=organization.pk,
            workspace_id=workspace.pk,
            uploaded_by_id=user.pk,
            file=f"media_library/{name}",
            filename=name,
            media_type="image",
            mime_type="image/png",
            **fields,
        )

    photo = asset("photo.png", thumbnail="media_library/thumbs/edited.jpg", width=400, height=300)
    crop = asset("photo-200x200.png", derived_from_id=photo.pk)
    crop_of_crop = asset("photo-100x100.png", derived_from_id=crop.pk)
    edited = old_version.objects.create(
        media_asset_id=photo.pk,
        version_number=1,
        file="media_library/versions/edited_1.jpg",
        thumbnail="media_library/thumbs/edited.jpg",
        file_size=1234,
        width=300,
        height=400,
        created_by_id=user.pk,
    )
    never_written = old_version.objects.create(
        media_asset_id=photo.pk, version_number=2, file="media_library/photo.png", created_by_id=user.pk
    )
    old_asset.objects.filter(pk=photo.pk).update(current_version_id=edited.pk)

    _migrate(_head())

    assert MediaAsset.objects.get(pk=crop.pk).original_id == photo.pk
    assert MediaAsset.objects.get(pk=crop_of_crop.pk).original_id == photo.pk
    copy = MediaAsset.objects.get(file="media_library/versions/edited_1.jpg")
    assert copy.original_id == photo.pk
    assert (copy.width, copy.height, copy.mime_type, copy.filename) == (300, 400, "image/jpeg", "photo-v1.jpg")
    assert copy.workspace_id == workspace.pk
    assert not MediaAssetVersion.objects.filter(pk=edited.pk).exists()
    assert MediaAssetVersion.objects.filter(pk=never_written.pk).exists()
    original = MediaAsset.objects.get(pk=photo.pk)
    assert original.file.name == "media_library/photo.png"
    assert original.thumbnail.name == ""
    assert original.current_version_id is None
    assert original.original_id is None
