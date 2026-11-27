"""Group existing copies under their original, and turn old edit versions into copies.

Crops already were copies (``derived_from``); they now also name the original
of their chain in ``original``. An edit made in the library used to live only
as a ``MediaAssetVersion`` row whose file never reached the asset, so posts
kept publishing the unedited file. Each version with its own edited file
becomes a copy of the asset, as edits are made from now on, and its row goes:
the copy owns that file now. A version whose edit was never written (its file
is still the asset's own) is left alone.
"""

import os

from django.db import migrations


def _root_id(asset_id, parents):
    seen = set()
    while parents.get(asset_id) and asset_id not in seen:
        seen.add(asset_id)
        asset_id = parents[asset_id]
    return asset_id


def group_and_convert(apps, schema_editor):
    MediaAsset = apps.get_model("media_library", "MediaAsset")
    MediaAssetVersion = apps.get_model("media_library", "MediaAssetVersion")

    parents = dict(MediaAsset.objects.filter(derived_from__isnull=False).values_list("pk", "derived_from_id"))
    for asset_id in parents:
        MediaAsset.objects.filter(pk=asset_id).update(original_id=_root_id(asset_id, parents))

    converted = []
    for version in MediaAssetVersion.objects.select_related("media_asset").order_by("created_at"):
        asset = version.media_asset
        if not version.file or version.file.name == asset.file.name:
            continue
        stem, _ext = os.path.splitext(asset.filename or "image")
        file_ext = os.path.splitext(version.file.name)[1].lower()
        copy = MediaAsset.objects.create(
            organization_id=asset.organization_id,
            workspace_id=asset.workspace_id,
            folder_id=asset.folder_id,
            uploaded_by_id=version.created_by_id or asset.uploaded_by_id,
            file=version.file.name,
            thumbnail=version.thumbnail.name if version.thumbnail else "",
            filename=f"{stem}-v{version.version_number}{file_ext}",
            media_type=asset.media_type,
            mime_type="image/jpeg" if file_ext in (".jpg", ".jpeg") else asset.mime_type,
            file_size=version.file_size,
            width=version.width or 0,
            height=version.height or 0,
            alt_text=asset.alt_text,
            title=asset.title,
            tags=list(asset.tags or []),
            source=asset.source,
            source_url=asset.source_url,
            attribution=asset.attribution,
            author_name=asset.author_name,
            author_url=asset.author_url,
            processing_status="completed",
            original_id=asset.original_id or asset.pk,
        )
        MediaAsset.objects.filter(pk=copy.pk).update(created_at=version.created_at)
        # The edit task showed the edited thumbnail on the original; give the
        # original its own look back (the card falls back to the file).
        if version.thumbnail and asset.thumbnail and asset.thumbnail.name == version.thumbnail.name:
            MediaAsset.objects.filter(pk=asset.pk).update(thumbnail="")
        converted.append(version.pk)

    MediaAsset.objects.filter(current_version_id__in=converted).update(current_version=None)
    MediaAssetVersion.objects.filter(pk__in=converted).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("media_library", "0007_asset_original"),
    ]

    operations = [
        # Not reversible into versions; going back leaves the copies as files.
        migrations.RunPython(group_and_convert, reverse_code=migrations.RunPython.noop),
    ]
