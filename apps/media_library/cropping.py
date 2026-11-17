"""The image editor's crop, as views receive it: the posted box, and a cropped copy or the reason not.

Shared by every view behind ``composer/partials/_crop_editor.html`` (the composer,
the idea window, the task board's delivered arts). Refusals are plain-text 400s:
the editor shows the body of a failed request as its error.
"""

from __future__ import annotations

from django.http import HttpResponse
from django.utils.translation import gettext as _


def crop_box(post) -> dict | None:
    """The crop box posted by the image editor (``x``, ``y``, ``width``, ``height``, source pixels), or None."""
    try:
        return {key: int(round(float(post[key]))) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None


def _plain(message: str, status: int = 400) -> HttpResponse:
    return HttpResponse(message, status=status, content_type="text/plain")


def quota_text_response(exc) -> HttpResponse:
    """A storage-cap breach in plain text: the workspace cap's own remedy, or the generic line."""
    from apps.billing.storage_usage import WorkspaceStorageExceededError

    if isinstance(exc, WorkspaceStorageExceededError):
        return _plain(str(exc.message))
    return _plain(_("Storage quota exceeded. Free up space or upgrade your plan."))


def crop_from_request(request, workspace, asset, *, quota_response=quota_text_response):
    """Crop ``asset`` into a new asset with the posted box: ``(cropped, None)`` or ``(None, response)``.

    The copy is made by ``services.crop_into_new_asset`` (same folder as the source, counted
    in the storage quotas). ``quota_response(exc)`` answers a storage-cap breach, for views
    whose front end expects another shape.
    """
    from apps.billing.storage_usage import WorkspaceStorageExceededError

    from .models import MediaAsset
    from .quotas import StorageQuotaExceededError
    from .services import ImageTooLargeError, crop_into_new_asset

    if asset is None or asset.media_type != MediaAsset.MediaType.IMAGE:
        return None, _plain(_("Only images can be cropped."))
    box = crop_box(request.POST)
    if box is None:
        return None, _plain(_("Choose the area to keep."))
    try:
        cropped = crop_into_new_asset(asset, workspace=workspace, uploaded_by=request.user, **box)
    except (StorageQuotaExceededError, WorkspaceStorageExceededError) as exc:
        return None, quota_response(exc)
    except ValueError:
        return None, _plain(_("The area to keep must be inside the image."))
    except ImageTooLargeError:
        return None, _plain(_("This image is too large to edit."))
    return cropped, None
