"""Background tasks for media processing."""

import logging
import tempfile

from background_task import background
from django.core.files.base import File
from django.utils import timezone

from .models import MediaAsset, MediaAssetVersion
from .services import (
    ImageTooLargeError,
    apply_image_edits,
    extract_image_metadata,
    extract_video_metadata,
    generate_image_thumbnail,
    generate_video_thumbnail,
    trim_video,
)
from .storage import download_to_path

logger = logging.getLogger(__name__)


@background(schedule=0)
def process_media_asset(asset_id):
    """Process a newly uploaded media asset: extract metadata and generate thumbnail."""
    try:
        asset = MediaAsset.objects.get(pk=asset_id)
    except MediaAsset.DoesNotExist:
        logger.warning("MediaAsset %s not found for processing", asset_id)
        return

    asset.processing_status = MediaAsset.ProcessingStatus.PROCESSING
    asset.save(update_fields=["processing_status"])

    try:
        if asset.media_type in (MediaAsset.MediaType.IMAGE, MediaAsset.MediaType.GIF):
            _process_image(asset)
        elif asset.media_type == MediaAsset.MediaType.VIDEO:
            _process_video(asset)
        asset.processing_status = MediaAsset.ProcessingStatus.COMPLETED
        asset.save(update_fields=["processing_status", "width", "height", "duration", "thumbnail", "updated_at"])
    except ImageTooLargeError as exc:
        # Determinate, and the user can act on it, so say so at WARNING with the
        # dimensions rather than dumping a traceback. Still FAILED — an asset we
        # cannot thumbnail is not a processed asset. Keep the dimensions we did
        # read, so the library can show what was wrong with it.
        logger.warning("Media asset %s rejected: %s", asset_id, exc)
        asset.processing_status = MediaAsset.ProcessingStatus.FAILED
        asset.save(update_fields=["processing_status", "width", "height", "updated_at"])
    except Exception:
        logger.exception("Failed to process media asset %s", asset_id)
        asset.processing_status = MediaAsset.ProcessingStatus.FAILED
        asset.save(update_fields=["processing_status"])


def _process_image(asset):
    """Extract metadata and generate thumbnail for an image.

    ``ImageTooLargeError`` is allowed to propagate to ``process_media_asset``,
    which marks the asset FAILED. Swallowing it left the asset COMPLETED with
    0x0 dimensions and no thumbnail, which reads as "this worked" everywhere
    in the UI.
    """
    metadata = extract_image_metadata(asset.file)
    asset.width = metadata.get("width", 0)
    asset.height = metadata.get("height", 0)

    thumbnail = generate_image_thumbnail(asset.file)
    if thumbnail:
        asset.thumbnail.save(f"thumb_{asset.id}.jpg", thumbnail, save=False)


def _process_video(asset):
    """Extract metadata and generate thumbnail for a video."""
    with tempfile.NamedTemporaryFile(suffix=f".{asset.file_extension}", delete=False) as tmp:
        tmp_path = tmp.name
    download_to_path(asset.file, tmp_path)

    try:
        metadata = extract_video_metadata(tmp_path)
        asset.width = metadata.get("width", 0)
        asset.height = metadata.get("height", 0)
        if "duration_seconds" in metadata:
            asset.duration = metadata["duration_seconds"]

        thumbnail = generate_video_thumbnail(tmp_path)
        if thumbnail:
            asset.thumbnail.save(f"thumb_{asset.id}.jpg", thumbnail, save=False)
    finally:
        import os

        os.unlink(tmp_path)


@background(schedule=0)
def process_image_edit(version_id, operations):
    """Apply image edits to create a new version file."""
    try:
        version = MediaAssetVersion.objects.select_related("media_asset").get(pk=version_id)
    except MediaAssetVersion.DoesNotExist:
        logger.warning("MediaAssetVersion %s not found", version_id)
        return

    # ``create_version`` seeds the row by assigning the asset's own FieldFile,
    # which copies the NAME rather than the bytes — until the edit is written,
    # version.file and asset.file are the same stored object. Remember it so the
    # cleanup below can tell "a file this task generated" from "the shared
    # source", and never delete the latter out from under the asset.
    source_name = version.file.name

    try:
        edited_file, (width, height) = apply_image_edits(version.media_asset.file, operations)

        version.file.save(f"edited_{version.id}.jpg", edited_file, save=False)
        version.width = width
        version.height = height
        version.file_size = edited_file.size if hasattr(edited_file, "size") else len(edited_file.read())
        version.save(update_fields=["file", "width", "height", "file_size"])

        thumbnail = generate_image_thumbnail(version.file)
        if thumbnail:
            version.thumbnail.save(f"thumb_v{version.id}.jpg", thumbnail, save=False)
            version.save(update_fields=["thumbnail"])

        asset = version.media_asset
        asset.width = width
        asset.height = height
        asset.thumbnail = version.thumbnail
        asset.save(update_fields=["width", "height", "thumbnail", "updated_at"])

    except ImageTooLargeError as exc:
        # The version row exists only to hold the edit result — ``create_version``
        # seeds it with a copy of the source file — so a deterministic failure
        # would otherwise leave a version that looks like an unchanged duplicate
        # and will never become anything else. Retrying cannot help: the image is
        # the size it is.
        #
        # ``create_version`` also pointed the asset at this row, and the FK is
        # SET_NULL, so rewind to the version it superseded first. Deleting
        # without that would leave an edited asset with no current version at
        # all, which is a worse state than the one we are cleaning up.
        logger.warning("Image edit for version %s rejected: %s", version_id, exc)
        asset = version.media_asset

        # Django does not delete FileField objects when a row goes, so dropping
        # the version without this strands whatever was already written. It is
        # reachable: ``apply_image_edits`` checks the SOURCE size, so an
        # upscaling resize can succeed and then produce output too large to
        # thumbnail, by which point version.file is already saved.
        if version.thumbnail:
            version.thumbnail.delete(save=False)
        if version.file and version.file.name != source_name:
            version.file.delete(save=False)

        previous = asset.versions.exclude(pk=version.pk).order_by("-version_number").first()
        version.delete()
        asset.current_version = previous
        asset.save(update_fields=["current_version", "updated_at"])
    except Exception:
        logger.exception("Failed to process image edit for version %s", version_id)


@background(schedule=0)
def trim_video_copy(
    copy_id, start_seconds, end_seconds, max_seconds=None, max_bytes=None, platform_post_ids=None, fit_for_phone=False
):
    """Fill the empty copy ``services.start_video_trim`` made with the trimmed video.

    Runs once per copy: anything but a ``processing`` copy is left alone, so a
    retried task changes nothing. The source is read, never written. The
    result is checked against the real file (length measured by ffprobe,
    size against ``max_bytes``, re-encoded once smaller when it is over) and
    against the storage quotas before it is stored. Ready, the copy is what
    the ``platform_post_ids`` rows still open for editing publish in place of
    the source. A failure marks the copy failed with a code
    (``processing_error``) that the composer and the library show.
    """
    import os
    import subprocess

    from apps.billing.storage_usage import WorkspaceStorageExceededError

    from .quotas import StorageQuotaExceededError, enforce_upload_quotas
    from .services import MIN_TRIM_SECONDS, VideoTrimError
    from .validators import storage_filename

    try:
        copy = MediaAsset.objects.select_related("derived_from", "organization", "workspace").get(pk=copy_id)
    except MediaAsset.DoesNotExist:
        logger.warning("Trimmed copy %s not found", copy_id)
        return
    if copy.processing_status != MediaAsset.ProcessingStatus.PROCESSING:
        return

    import time

    # Read by the composer's trimmer to draw its bar: 0-10 getting the
    # original, 10-90 ffmpeg's own count, 90-100 storing the result. Written
    # at most about once a second, never backwards within a pass.
    last = {"value": 0, "at": 0.0}

    def report(value, *, force=False):
        value = int(max(0, min(100, value)))
        now = time.monotonic()
        if force or (value != last["value"] and now - last["at"] >= 1.0):
            # updated_at too: it is the heartbeat fail_stalled_video_trims reads.
            MediaAsset.objects.filter(pk=copy.pk).update(processing_progress=value, updated_at=timezone.now())
            last.update(value=value, at=now)

    report(2, force=True)
    paths = []
    try:
        source = copy.derived_from
        if source is None or not source.file:
            raise VideoTrimError("trim_source")
        for suffix in (f".{source.file_extension or 'mp4'}", ".mp4"):
            fd, path = tempfile.mkstemp(suffix=suffix, prefix="connektflow_trim_")
            os.close(fd)
            paths.append(path)
        input_path, output_path = paths
        try:
            download_to_path(source.file, input_path)
        except Exception as exc:  # noqa: BLE001 - storage backends raise varied errors when the object is gone
            raise VideoTrimError("trim_source") from exc
        report(10, force=True)

        real_duration = extract_video_metadata(input_path).get("duration_seconds") or source.duration
        if not real_duration:
            raise VideoTrimError("trim_source")
        end_seconds = min(float(end_seconds), real_duration)
        if end_seconds - float(start_seconds) < MIN_TRIM_SECONDS:
            raise VideoTrimError("trim_range")

        # A higher CRF only once, and only when a size limit is in play: the
        # trimmed stretch usually fits, and a second pass doubles the wait.
        for crf in (23, 28) if max_bytes else (23,):
            trim_video(
                input_path,
                output_path,
                start_seconds,
                end_seconds,
                crf=crf,
                fit_for_phone=bool(fit_for_phone),
                on_progress=lambda f: report(10 + 80 * f),
            )
            size = os.path.getsize(output_path)
            if not max_bytes or size <= max_bytes:
                break
        else:
            raise VideoTrimError("trim_too_large")

        metadata = extract_video_metadata(output_path)
        duration = metadata.get("duration_seconds") or (end_seconds - float(start_seconds))
        if max_seconds and duration > float(max_seconds) + 0.5:
            raise VideoTrimError("trim_failed")

        report(90, force=True)
        try:
            enforce_upload_quotas(copy.organization, copy.workspace, size)
        except (StorageQuotaExceededError, WorkspaceStorageExceededError) as exc:
            raise VideoTrimError("trim_quota") from exc

        with open(output_path, "rb") as f:
            name = storage_filename(copy.filename, "video/mp4")
            copy.file.save(name, File(f, name=name), save=False)
        copy.file_size = size
        copy.duration = duration
        copy.width = metadata.get("width") or source.width
        copy.height = metadata.get("height") or source.height
        thumbnail = generate_video_thumbnail(output_path)
        if thumbnail:
            copy.thumbnail.save(f"thumb_{copy.id}.jpg", thumbnail, save=False)
        copy.processing_status = MediaAsset.ProcessingStatus.COMPLETED
        copy.processing_error = ""
        copy.processing_progress = 100
        copy.save()
    except VideoTrimError as exc:
        _fail_trim(copy, exc.code)
        return
    except subprocess.TimeoutExpired:
        _fail_trim(copy, "trim_timeout")
        return
    except Exception:
        logger.exception("Failed to trim video into copy %s", copy_id)
        _fail_trim(copy, "trim_failed")
        return
    finally:
        for path in paths:
            if os.path.exists(path):
                os.unlink(path)

    if platform_post_ids:
        from apps.composer.models import PlatformPost
        from apps.composer.services import set_format_crop

        rows = list(
            PlatformPost.objects.filter(pk__in=platform_post_ids, post__workspace_id=copy.workspace_id).exclude(
                status__in=PlatformPost.PROTECTED_STATUSES
            )
        )
        if rows:
            set_format_crop(rows, copy)


def _fail_trim(copy, code):
    copy.processing_status = MediaAsset.ProcessingStatus.FAILED
    copy.processing_error = code
    copy.save(update_fields=["processing_status", "processing_error", "updated_at"])


# How often fail_stalled_video_trims looks, and how long a trim may go silent
# (no progress written) or wait in the queue before its copy is called failed.
STALLED_TRIM_SWEEP_INTERVAL_SECONDS = 10 * 60
STALLED_TRIM_SILENCE_SECONDS = 15 * 60
STALLED_TRIM_QUEUE_SECONDS = 60 * 60


@background(schedule=0)
def fail_stalled_video_trims():
    """Mark failed the trimmed copies whose trim will never finish.

    A copy waits ``processing`` while ``trim_video_copy`` runs, and the
    composer keeps the format held back meanwhile. If the media worker dies
    mid-encode (an out-of-memory kill under its container cap, a deploy) or
    never runs, the copy would stay ``processing`` for good and the trimmer
    would wait forever. Failed here, the person sees "the processing
    stopped" and can trim again:

    - its job is gone (finished or dropped without settling the copy);
    - its job was taken and the copy has written no progress for
      ``STALLED_TRIM_SILENCE_SECONDS`` (the task updates it every second);
    - its job has waited more than ``STALLED_TRIM_QUEUE_SECONDS`` without
      starting (no media worker running).

    Runs on the general worker, so it still works when the media one is down.
    """
    from datetime import timedelta

    from background_task.models import Task

    now = timezone.now()
    stalled = []
    copies = MediaAsset.objects.filter(
        processing_status=MediaAsset.ProcessingStatus.PROCESSING, derived_from__isnull=False, file=""
    ).only("pk", "updated_at", "created_at")
    for copy in copies:
        job = Task.objects.filter(
            task_name="apps.media_library.tasks.trim_video_copy", task_params__contains=str(copy.pk)
        ).first()
        if job is None:
            # Created a moment ago: its job row may not be visible yet.
            if now - copy.created_at > timedelta(minutes=1):
                stalled.append(copy.pk)
        elif job.locked_by:
            if now - copy.updated_at > timedelta(seconds=STALLED_TRIM_SILENCE_SECONDS):
                stalled.append(copy.pk)
        elif now - copy.created_at > timedelta(seconds=STALLED_TRIM_QUEUE_SECONDS):
            stalled.append(copy.pk)
    if stalled:
        MediaAsset.objects.filter(pk__in=stalled, processing_status=MediaAsset.ProcessingStatus.PROCESSING).update(
            processing_status=MediaAsset.ProcessingStatus.FAILED, processing_error="trim_stalled", updated_at=now
        )
        logger.warning("Marked %s stalled video trim(s) failed: %s", len(stalled), stalled)


# How often the recurring pending-upload sweep runs; registered on a repeating
# schedule by apps.media_library.apps.MediaLibraryConfig.
PENDING_UPLOAD_SWEEP_INTERVAL_SECONDS = 60 * 60  # hourly


@background(schedule=0)
def sweep_pending_uploads():
    """Delete expired, never-finalized presigned uploads and their objects.

    ``request_media_upload`` presigns a key and writes a ``PendingUpload`` row; if
    the agent never finalizes, the row (and any partially-uploaded object) would
    linger. This reaps anything past its expiry that was never finalized. Object
    deletion is best-effort — the object may never have been uploaded at all.
    """
    import contextlib

    from django.utils import timezone

    from .models import PendingUpload
    from .storage import delete_object

    stale = PendingUpload.objects.filter(finalized_at__isnull=True, expires_at__lt=timezone.now())
    for pending in stale.iterator():
        with contextlib.suppress(Exception):
            delete_object(pending.storage_key)
        pending.delete()


# How often the recurring orphaned-media sweep runs; registered on a repeating
# schedule by apps.media_library.apps.MediaLibraryConfig. Replaces the daily
# docker-compose ``maintenance`` loop so it runs on every deploy target.
ORPHANED_MEDIA_SWEEP_INTERVAL_SECONDS = 24 * 60 * 60  # daily


@background(schedule=0)
def run_orphaned_media_sweep():
    """Delete media assets no longer referenced by any post, idea, or template.

    Wraps ``services.sweep_orphaned_media`` (the same code path as the
    ``cleanup_orphaned_media`` command) so the cleanup runs on the shared
    ``process_tasks`` worker everywhere, not just the VPS maintenance container.
    """
    from .services import sweep_orphaned_media

    # min_age_days falls through to services.ORPHANED_MEDIA_MIN_AGE_DAYS (the
    # single source), matching the management command's default.
    sweep_orphaned_media(log=logger.info)


@background(schedule=0)
def remove_stored_files(names):
    """Delete from storage the files of a workspace or organization that was deleted.

    Raises when any file could not be deleted so the task is retried and the
    failure stays on its row; deleting an already-deleted file is a no-op.
    """
    from .services import delete_stored_files

    result = delete_stored_files(names)
    if result["errors"]:
        raise RuntimeError(f"{result['errors']} of {len(names)} stored files could not be deleted")
