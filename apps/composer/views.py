"""Views for the Post Composer (F-2.1)."""

import base64
import contextlib
import json
import logging
import re
import uuid
from datetime import UTC, date, datetime, time
from urllib.parse import urljoin

import httpx
from dateutil import parser as date_parser
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache, caches
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.core.paginator import Paginator
from django.db import models, transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.html import format_html, strip_tags
from django.utils.text import get_text_list
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy, ngettext, override, pgettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.billing.storage_usage import WorkspaceStorageExceededError
from apps.common.htmx import toast_response
from apps.common.validators import (
    is_safe_url,
    parse_and_truncate_tag_string,
    parse_and_truncate_youtube_tag_string,
    safe_xml_fromstring,
)
from apps.media_library.models import MediaAsset
from apps.media_library.quotas import StorageQuotaExceededError
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.publisher.manual_delivery import NOTIFY_DELIVERY, is_notify_delivery
from apps.social_accounts.models import PlatformVisibility, SocialAccount
from apps.workspaces.models import Workspace
from providers import is_video_only, max_images_per_post, requires_media
from providers.exceptions import ProviderError
from providers.google_business import PROFILE_PHOTO_CATEGORIES
from providers.tiktok import VALID_PRIVACY_LEVELS as TIKTOK_PRIVACY_LEVELS

from . import idea_icons
from .emoji import emoji_catalogue, facebook_feeling_choices
from .forms import ContentCategoryForm, PostForm, TemplateSubmissionForm
from .models import (
    ContentCategory,
    Feed,
    Idea,
    IdeaGroup,
    IdeaMedia,
    IdeaTemplate,
    PlatformPost,
    Post,
    PostMedia,
    PostTemplate,
    PostVersion,
    Tag,
    TagGroup,
)
from .services import (
    MAX_IDEA_TAGS,
    MAX_PENDING_TEMPLATE_SUBMISSIONS,
    TagGroupNameTakenError,
    TemplateSubmissionLimitError,
    add_to_tag_group,
    brand_hashtags,
    can_edit_post,
    channel_accounts,
    clean_channel_formats,
    delete_tag,
    ensure_tags,
    format_blocker,
    format_media_replacements,
    image_count_blocker,
    media_blocker,
    media_change_blocker,
    needs_approval_to_schedule,
    parse_channel_formats,
    platforms_for_template_networks,
    post_image_count,
    post_media_types,
    post_ordered_media_types,
    publish_blocker,
    record_template_use,
    require_publishable,
    revert_approved_to_review,
    reviewed_content_changed,
    reviewed_content_snapshot,
    save_tag_group,
    submit_template,
    tag_group_payload,
    tag_groups_payload,
    tag_usage,
    template_account_formats,
    template_shelf,
    template_text,
    templates_for,
    toggle_template_favorite,
    usable_media_types,
)
from .status import READONLY_STATUSES, derive_post_status

logger = logging.getLogger(__name__)

MAX_CSV_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB cap on CSV planner imports

# Facebook's feed grid tops out at five tiles; anything past that is counted
# on the fifth as "+N". Mirrored in the preview so the composer shows what
# the feed will show.
FACEBOOK_GRID_MAX_TILES = 5


# Frame of the preview slide per format (the ordinary post keeps its media's own shape).
PREVIEW_FORMAT_RATIOS = PlatformPost.FORMAT_RATIOS


def _preview_media_item(asset):
    """One attachment as the preview templates expect it."""
    return {
        "url": asset.file.url if asset.file else "",
        "is_video": asset.is_video,
        "filename": asset.filename,
        # Facebook picks its grid from the first photo's shape, so the
        # preview needs the orientation to pick the same one.
        "is_portrait": bool(asset.width and asset.height and asset.height > asset.width),
    }


def _facebook_grid_context(media_items):
    """Tiles the Facebook preview shows, and the count hidden behind the last."""
    return {
        "fb_grid_items": media_items[:FACEBOOK_GRID_MAX_TILES],
        "fb_grid_overflow": max(0, len(media_items) - FACEBOOK_GRID_MAX_TILES),
    }


# Shown when every posting slot within the lookahead horizon is already taken.
# Lazy: this is evaluated at import, before a request has picked the language.
_QUEUE_FULL_MSG = gettext_lazy("No open posting slot within the scheduling horizon — add posting slots or free one up.")


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    has_membership = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=workspace,
    ).exists()
    if not has_membership:
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


def _member_permissions(request):
    """The permissions of the member making the request, in this workspace."""
    membership = request.workspace_membership
    return membership.effective_permissions if membership else {}


def _require_post_editor(request, post):
    """Refuse a member who neither wrote ``post`` nor may edit other members' posts."""
    if not can_edit_post(post, request.user, _member_permissions(request)):
        raise PermissionDenied("You do not have permission to edit this post.")


def _send_back_for_review(request, post):
    """Return ``post`` to review after a change to content a reviewer cleared.

    Approved rows always go back. Scheduled rows go back too when the member
    who made the change may only schedule approved content.
    """
    return revert_approved_to_review(
        post, include_scheduled=needs_approval_to_schedule(post.workspace, _member_permissions(request))
    )


def _is_valid_uuid(value):
    try:
        uuid.UUID(value)
    except (ValueError, TypeError):
        return False
    return True


def _parse_selected_account_ids(raw):
    """Split a comma-separated ``selected_accounts`` value into account IDs.

    Non-UUID entries are dropped: they can only come from a crafted or
    corrupted request, and letting them reach a UUIDField lookup raises
    ValidationError (an unhandled 500) instead of being ignored.
    """
    return [s.strip() for s in (raw or "").split(",") if s.strip() and _is_valid_uuid(s.strip())]


def _get_account_scope(request):
    """Return the validated ``account_scope`` POST value, or ``None`` when unscoped.

    The hidden input is server-rendered from a UUID-validated ``?account=``
    param, so a malformed value means a crafted or corrupted request —
    reject it outright (HTTP 400) rather than guessing which rows to touch.
    """
    scope = request.POST.get("account_scope", "").strip()
    if not scope:
        return None
    if not _is_valid_uuid(scope):
        raise SuspiciousOperation("Malformed account_scope.")
    return scope


def _remove_deselected_platform_posts(request, post, targets):
    """Delete PlatformPosts the user deselected in the composer form.

    ``targets`` maps each selected account id to the formats chosen for it; a
    row whose account or format is not among them was deselected.

    When the composer was opened scoped to a single account
    (``?account=`` → hidden ``account_scope`` input), the form only renders
    that account, so ``targets`` is NOT the complete desired set —
    restrict deletion to the scoped account to keep siblings intact.
    Published/publishing rows are never deleted by deselection (explicit
    post deletion remains the user's call — see PlatformPost.PROTECTED_STATUSES).
    """
    qs = post.platform_posts.exclude(status__in=PlatformPost.PROTECTED_STATUSES)
    scope = _get_account_scope(request)
    if scope:
        qs = qs.filter(social_account_id=scope)
    stale = [
        pp.pk
        for pp in qs.only("id", "social_account_id", "format")
        if pp.format not in targets.get(str(pp.social_account_id), ())
    ]
    if stale:
        PlatformPost.objects.filter(pk__in=stale).delete()


def _selected_targets(request, post, accounts):
    """``{account_id: [format, ...]}`` for the selected ``accounts``.

    The composer posts ``selected_targets`` as ``"<account>:<format>,..."``.
    A format the account's platform doesn't offer is dropped, and an account
    left with none publishes its ordinary post. Saves that don't come from the
    composer's picker (no ``selected_targets`` field) keep the formats each
    account already has on the post.
    """
    raw = request.POST.get("selected_targets")
    chosen: dict[str, list[str]] = {}
    if raw is None:
        if post is not None and not post._state.adding:
            for acc_id, fmt in post.platform_posts.values_list("social_account_id", "format"):
                chosen.setdefault(str(acc_id), []).append(fmt)
    else:
        for item in raw.split(","):
            acc_id, _sep, fmt = item.strip().partition(":")
            if acc_id and fmt and fmt not in chosen.get(acc_id, []):
                chosen.setdefault(acc_id, []).append(fmt)
    targets = {}
    for account in accounts:
        acc_id = str(account.id)
        formats = [f for f in account.formats if f in chosen.get(acc_id, [])]
        targets[acc_id] = formats or [PlatformPost.Format.DEFAULT]
    return targets


def _scoped_platform_post_ids(request, post):
    """PlatformPost IDs inside the composer's ``account_scope``, or ``None`` when unscoped."""
    scope = _get_account_scope(request)
    if not scope:
        return None
    return list(post.platform_posts.filter(social_account_id=scope).values_list("id", flat=True))


def _match_scope(children, scope):
    """The PlatformPosts in ``children`` belonging to ``scope``, an account UUID.

    Returns every child when ``scope`` is empty, and none when it is set but
    unparseable. Compares UUID *objects*, never their strings: ``uuid.UUID``
    accepts any case, but ``str()`` of one is always lowercase, so a
    differently-cased ``?account=`` would silently match nothing.
    """
    if not scope:
        return list(children)
    try:
        wanted = uuid.UUID(str(scope))
    except (ValueError, TypeError):
        return []
    return [pp for pp in children if pp.social_account_id == wanted]


def _gated_children(children, scope):
    """The children a read-only decision should weigh, given ``scope``.

    Like ``_match_scope``, except an unmatched scope falls back to the whole
    list. That fallback is load-bearing: without it ``?account=<id of an
    account with no row on this post>`` would leave the caller looking at an
    empty list, which reads as "nothing published here" and reopens a live post
    for editing. An unmatched scope must never be *more* permissive than no
    scope at all.
    """
    return _match_scope(children, scope) or list(children)


def _network_delete_context(children):
    """What the composer's delete dialog says about the posts already live:
    the networks ``children`` can be deleted from, and those they stay on."""
    networks: dict[str, list[str]] = {"yes": [], "no": []}
    for child in children:
        names = networks.get(child.network_delete)
        label = str(child.social_account.get_platform_display())
        if names is not None and label not in names:
            names.append(label)
    return {"delete_networks_yes": networks["yes"], "delete_networks_no": networks["no"]}


def _children_are_locked(children):
    """Whether every one of ``children`` is published or mid-publish.

    The *server's* rule, used by the save and autosave endpoints: a row in
    ``PROTECTED_STATUSES`` can no longer be written, so when they all are there
    is nothing left to save. Deliberately narrower than
    ``_children_are_readonly`` — see there.
    """
    return bool(children) and all(pp.status in PlatformPost.PROTECTED_STATUSES for pp in children)


def _children_are_readonly(children):
    """Whether the composer should stop *offering* to change ``children``.

    The *UI's* rule, and wider than ``_children_are_locked``: a
    partially-published post still holds a writable failed child, so the server
    keeps accepting scoped writes to it, but the aggregate is a finished
    publish and the action bar steps back and points at Clone instead.

    The two rules disagree on exactly that case, on purpose. The UI is the
    stricter side, so the asymmetry only ever hides an affordance — it never
    lets a write through that ``_children_are_locked`` would refuse. Change one
    and you almost certainly need to think about the other.
    """
    return derive_post_status([pp.status for pp in children]) in READONLY_STATUSES


def _is_readonly_write(request, post):
    """Whether every PlatformPost this request would touch has already published.

    Backs the composer's read-only banner on the server: once a post is live (or
    mid-publish) there is nothing left to write, so the save and autosave paths
    must stop rewriting its content and re-stamping ``scheduled_at``.

    Scoped requests (``account_scope``) weigh only that child, mirroring the
    scoped composer: a failed channel on a partially-published post stays
    workable, and Clone remains the retry path for the published ones. A post
    with no children yet (a brand-new draft) is never read-only.
    """
    # Only three columns are needed; skip hydrating captions and platform_extra.
    children = list(post.platform_posts.only("id", "status", "social_account_id"))
    return _children_are_locked(_gated_children(children, _get_account_scope(request)))


def _readonly_rejection(request, post):
    """A 400 for a write to an already-published post, or ``None`` to proceed.

    ``{"errors": {...}}`` is the only shape the composer renders as a readable
    message (see ``onFormSaved`` in compose.html); ``PermissionDenied`` would
    return Django's HTML 403 and degrade to the generic fallback toast.
    """
    if not _is_readonly_write(request, post):
        return None
    return JsonResponse(
        {
            "errors": {
                "status": "This post has already published and can't be edited. Use Clone to repost an editable copy."
            }
        },
        status=400,
    )


def _apply_facebook_feed_extras(request, extra, acc_id):
    """Record the Facebook customize card's place and feeling on ``extra``.

    Gated on its own marker rather than the panel's: these live in the
    per-platform customize card, which the user opens on demand, so their
    fields are absent from a save where it was never opened. Clearing on
    absence would make every ordinary save wipe a place set earlier.

    Once the card *is* open, an empty field does clear the stored value —
    that is how a place is removed.

    The feeling is validated against the ids we offer; the place is a Page id
    we only ever got from our own search endpoint.
    """
    from providers.meta_feed_extras import FEELING_IDS

    if f"facebook_custom_{acc_id}" not in request.POST:
        return

    _apply_place(request, extra, acc_id, "facebook")

    feeling_id = request.POST.get(f"facebook_feeling_{acc_id}", "").strip()
    if feeling_id in FEELING_IDS:
        extra["feeling_object_id"] = feeling_id
    else:
        extra.pop("feeling_object_id", None)


def _apply_place(request, extra, acc_id, prefix):
    """Record the place picker's choice (``<prefix>_place_id_<account>``) on ``extra``.

    The id is a location Page we only ever got from our own search endpoint;
    the name is kept so the composer can show the choice without asking Meta
    again. An empty field removes the place.
    """
    place_id = request.POST.get(f"{prefix}_place_id_{acc_id}", "").strip()
    place_name = request.POST.get(f"{prefix}_place_name_{acc_id}", "").strip()
    if place_id:
        extra["place_id"] = place_id
        extra["place_name"] = place_name
    else:
        extra.pop("place_id", None)
        extra.pop("place_name", None)


def _apply_instagram_extras(request, extra, acc_id):
    """Record the Instagram card's collaborators, tagged people and location.

    Account-wide like the Facebook place: written to every format of the
    account, and the provider sends each one only where Instagram takes it
    (no collaborators on a Story, no tags on a Reel). Gated on the card's own
    marker for the same reason as ``_apply_facebook_feed_extras``.
    """
    from providers.meta_feed_extras import IG_MAX_COLLABORATORS, IG_MAX_USER_TAGS, parse_instagram_usernames

    if f"instagram_custom_{acc_id}" not in request.POST:
        return

    for key, limit in (("collaborators", IG_MAX_COLLABORATORS), ("user_tags", IG_MAX_USER_TAGS)):
        usernames = parse_instagram_usernames(request.POST.get(f"instagram_{key}_{acc_id}", ""), limit=limit)
        if usernames:
            extra[key] = usernames
        else:
            extra.pop(key, None)
    _apply_place(request, extra, acc_id, "instagram")

    # A Reel's cover (a moment of the video) and whether it also shows on the
    # profile grid. The checkbox is only on screen while Reels is chosen.
    if f"instagram_reel_cover_ms_{acc_id}" in request.POST:
        cover_ms = _cover_timestamp_ms(request.POST.get(f"instagram_reel_cover_ms_{acc_id}", ""))
        if cover_ms is None:
            extra.pop("video_cover_timestamp_ms", None)
        else:
            extra["video_cover_timestamp_ms"] = cover_ms
        extra["share_to_feed"] = bool(request.POST.get(f"instagram_share_to_feed_{acc_id}"))


def _cover_timestamp_ms(value):
    """A frame picker timestamp in milliseconds, or None when blank or invalid.

    Parsed with int() rather than str.isdigit(): isdigit() accepts non-ASCII
    digits (e.g. "²", "١٢") that int() then rejects.
    """
    try:
        ms = int(str(value or "").strip())
    except ValueError:
        return None
    return ms if ms >= 0 else None


# post_type hints that name a format. The row's ``format`` says this now, so
# a leftover hint is dropped whenever the composer saves the row.
_FORMAT_HINTS = ("story", "reel", "profile_photo")


def _apply_format_extras(request, pp, acc_id):
    """Record the per-format choices the composer offers on ``pp``.

    A Story can be delivered by notifying someone to post it by hand; a
    Google Business photo has a gallery category. Only applied when the save
    comes from the composer's picker, which renders those controls.
    """
    if "selected_targets" not in request.POST:
        return
    extra = {**(pp.platform_extra or {})}
    if extra.get("post_type") in _FORMAT_HINTS:
        extra.pop("post_type")
    if pp.format == PlatformPost.Format.STORY and request.POST.get(f"story_delivery_{acc_id}") == NOTIFY_DELIVERY:
        extra["delivery"] = NOTIFY_DELIVERY
    else:
        extra.pop("delivery", None)
    if pp.format == PlatformPost.Format.PROFILE_PHOTO:
        category = request.POST.get(f"gbp_photo_category_{acc_id}", "").strip()
        extra["photo_category"] = category if category in PROFILE_PHOTO_CATEGORIES else "ADDITIONAL"
    else:
        extra.pop("photo_category", None)
    pp.platform_extra = extra


def _sync_platform_posts(request, post, workspace, initial_status=None):
    """Sync platform post selections from form data.

    When ``initial_status`` is given, newly-created PlatformPost rows are
    initialised to that status (e.g. ``"draft"``, ``"scheduled"``,
    ``"pending_review"``). Existing rows are not touched here — call
    ``_transition_post_children`` separately if you want to move them.
    """
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    accounts = list(SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace))
    targets = _selected_targets(request, post, accounts)
    _remove_deselected_platform_posts(request, post, targets)
    for account in accounts:
        for fmt in targets[str(account.id)]:
            _sync_platform_post(request, post, account, fmt, initial_status)


def _target_field(data, name, acc_id, fmt):
    """A composer field set per format (``<name>_<account>_<format>``).

    The composer's "Customize" chips are per format, so each format of an
    account can carry its own caption. Falls back to the account-wide
    ``<name>_<account>`` that other callers still send.
    """
    key = f"{name}_{acc_id}_{fmt}"
    if key in data:
        return data.get(key, "")
    return data.get(f"{name}_{acc_id}", "")


def _usable_asset_id(request, workspace, field):
    """The media asset id posted in ``field``, or ``None``.

    Only an asset of this workspace, or one shared across its organization,
    is kept: the publisher uploads the file the id names.
    """
    asset_id = request.POST.get(field, "").strip()
    if not _is_valid_uuid(asset_id):
        return None
    visible = MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id)
    return asset_id if visible.filter(id=asset_id).exists() else None


def _sync_platform_post(request, post, account, fmt, initial_status):
    """Create or update the row publishing ``post`` as ``fmt`` on ``account``."""
    acc_id = str(account.id)
    defaults = {}
    if initial_status:
        defaults["status"] = initial_status
    pp, _created = PlatformPost.objects.get_or_create(
        post=post,
        social_account=account,
        format=fmt,
        defaults=defaults,
    )
    override_title = _target_field(request.POST, "override_title", acc_id, fmt).strip()
    override_caption = _target_field(request.POST, "override_caption", acc_id, fmt).strip()
    override_comment = _target_field(request.POST, "override_comment", acc_id, fmt).strip()
    pp.platform_specific_title = override_title if override_title else None
    pp.platform_specific_caption = override_caption if override_caption else None
    pp.platform_specific_first_comment = override_comment if override_comment else None

    # Per-platform extras
    if account.platform == "facebook":
        # Place and feeling live in the customize card the user opens on
        # demand; see _apply_facebook_feed_extras for why it has its own
        # marker.
        extra = {**(pp.platform_extra or {})}
        _apply_facebook_feed_extras(request, extra, acc_id)
        pp.platform_extra = extra

    elif account.platform == "instagram":
        extra = {**(pp.platform_extra or {})}
        _apply_instagram_extras(request, extra, acc_id)
        pp.platform_extra = extra

    elif account.platform == "youtube":
        tags_list = parse_and_truncate_youtube_tag_string(request.POST.get(f"yt_tags_{acc_id}", ""))
        privacy_status = request.POST.get(f"yt_privacy_status_{acc_id}", "public")
        if privacy_status not in ("public", "unlisted", "private"):
            privacy_status = "public"
        thumb_id = _usable_asset_id(request, post.workspace, f"yt_thumbnail_asset_id_{acc_id}")
        pp.platform_extra = {
            "privacy_status": privacy_status,
            "self_declared_made_for_kids": request.POST.get(f"yt_made_for_kids_{acc_id}") == "true",
            "tags": tags_list,
            "thumbnail_asset_id": thumb_id,
        }

    elif account.platform == "pinterest":
        board_id = request.POST.get(f"pin_board_id_{acc_id}", "").strip()
        if not board_id:
            board_id = (pp.platform_extra or {}).get("board_id") or None
        pp.platform_extra = {
            "board_id": board_id,
            "link_url": request.POST.get(f"pin_link_url_{acc_id}", "").strip() or None,
            "alt_text": request.POST.get(f"pin_alt_text_{acc_id}", "").strip() or None,
            "tag_products": request.POST.get(f"pin_tag_products_{acc_id}", "").strip() or None,
            "allow_comments": request.POST.get(f"pin_allow_comments_{acc_id}") == "true",
            "show_similar_products": request.POST.get(f"pin_show_similar_{acc_id}") == "true",
            "cover_image_asset_id": _usable_asset_id(request, post.workspace, f"pin_cover_image_asset_id_{acc_id}"),
        }

    elif account.platform == "tiktok" and f"tiktok_privacy_level_{acc_id}" in request.POST:
        # Only rebuild extras when the TikTok panel was part of the form,
        # so non-composer saves can't wipe a previously chosen privacy level.
        privacy = request.POST.get(f"tiktok_privacy_level_{acc_id}", "").strip()
        if privacy not in TIKTOK_PRIVACY_LEVELS:
            # An empty/invalid submit (required-validation bypassed) must
            # not wipe a previously saved choice.
            privacy = (pp.platform_extra or {}).get("privacy_level", "")
        # Comment / Duet / Stitch are independent interaction settings —
        # TikTok's UX guidelines require a separate toggle per interaction,
        # each greyed out on its own when the creator disabled it.
        extra = {
            "disable_comment": request.POST.get(f"tiktok_allow_comment_{acc_id}") != "true",
            "disable_duet": request.POST.get(f"tiktok_allow_duet_{acc_id}") != "true",
            "disable_stitch": request.POST.get(f"tiktok_allow_stitch_{acc_id}") != "true",
            "brand_organic_toggle": request.POST.get(f"tiktok_brand_organic_{acc_id}") == "true",
            "brand_content_toggle": request.POST.get(f"tiktok_brand_content_{acc_id}") == "true",
            "is_aigc": request.POST.get(f"tiktok_is_aigc_{acc_id}") == "true",
        }
        if privacy:
            extra["privacy_level"] = privacy
        # Cover frame timestamp from the composer's frame picker; omitted
        # when blank/invalid so TikTok falls back to the first frame.
        cover_ms = _cover_timestamp_ms(request.POST.get(f"tiktok_video_cover_timestamp_ms_{acc_id}", ""))
        if cover_ms is not None:
            extra["video_cover_timestamp_ms"] = cover_ms
        pp.platform_extra = extra

    _apply_format_extras(request, pp, acc_id)
    pp.save()


def _validate_pinterest_board_selection(request, post, workspace):
    """Selected Pinterest accounts need a board before composer save/submit."""
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    if not selected_ids:
        return None

    accounts = SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace, platform="pinterest")
    for account in accounts:
        acc_id = str(account.id)
        board_id = request.POST.get(f"pin_board_id_{acc_id}", "").strip()
        if not board_id and post.pk:
            board_id = (
                PlatformPost.objects.filter(post=post, social_account=account)
                .values_list("platform_extra__board_id", flat=True)
                .first()
                or ""
            )
        if not board_id:
            return JsonResponse(
                {
                    "errors": {
                        "pinterest_board": _("Select a Pinterest board for %(account)s.")
                        % {"account": account.account_name}
                    }
                },
                status=400,
            )
    return None


# Actions that commit a post to publishing. A draft may still be waiting for
# its media, so saving one is never blocked on it.
_PUBLISH_COMMITTING_ACTIONS = frozenset(
    {
        "schedule",
        "publish_now",
        "add_to_queue",
        "add_to_queue_priority",
        "submit_for_approval",
        "resubmit_for_approval",
    }
)


def _pending_media_ids(request, post, workspace) -> list:
    """The session uploads this save will attach to ``post``.

    A post that has never been saved takes all of them. A saved one takes only
    those the form lists as ``pending_media_ids``: the media picker is loaded
    before the post exists, so when an autosave creates the post while it is
    open, what it inserts still lands in the session. The form shows those
    thumbnails and names them; another tab's leftovers are not named.
    """
    pending_ids = request.session.get(f"pending_media_{workspace.id}", [])
    if post._state.adding:
        return list(pending_ids)
    named = set(request.POST.getlist("pending_media_ids"))
    return [asset_id for asset_id in pending_ids if str(asset_id) in named]


def _composer_media_types(request, post, workspace) -> set[str]:
    """Usable media types on the post plus the uploads waiting to be attached."""
    media_types = post_media_types(post)
    # The uploads are attached by save_post after this check has run.
    pending_ids = _pending_media_ids(request, post, workspace)
    if pending_ids:
        media_types |= usable_media_types(MediaAsset.objects.filter(id__in=pending_ids, workspace=workspace))
    return media_types


def _composer_ordered_media_types(request, post, workspace) -> list[str]:
    """``post_ordered_media_types`` plus the uploads waiting to be attached."""
    saved = [] if post._state.adding else post_ordered_media_types(post)
    pending_ids = _pending_media_ids(request, post, workspace)
    if not pending_ids:
        return saved
    by_id = {
        str(asset.id): asset.media_type
        for asset in MediaAsset.objects.filter(id__in=pending_ids, workspace=workspace).exclude(file="")
    }
    return (saved + [by_id[str(i)] for i in pending_ids if str(i) in by_id])[:2]


def _validate_formats(request, post, workspace, action):
    """Refuse to schedule a post whose chosen formats the media can't be published as.

    Drafts are let through: the format is picked before the media, so a draft
    may still be waiting for it; the composer shows the mismatch meanwhile.
    """
    if action not in _PUBLISH_COMMITTING_ACTIONS:
        return None
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    if not selected_ids or "selected_targets" not in request.POST:
        return None
    accounts = list(SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace))
    targets = _selected_targets(request, post, accounts)
    if all(formats == [PlatformPost.Format.DEFAULT] for formats in targets.values()):
        return None
    media = _composer_ordered_media_types(request, post, workspace)
    for account in accounts:
        formats = targets[str(account.id)]
        for fmt in formats:
            reason = format_blocker(fmt, media)
            if reason:
                return JsonResponse(
                    {"errors": {"media": f"{account.account_name}: {reason}"}},
                    status=400,
                )
        # Instagram publishes a lone video in the feed as a Reel, so choosing
        # both would publish the same Reel twice.
        if (
            account.platform in ("instagram", "instagram_login")
            and {"default", "reel"} <= set(formats)
            and media == ["video"]
        ):
            return JsonResponse(
                {
                    "errors": {
                        "media": _(
                            "%(account)s: Instagram publishes a single video in the feed as a Reel. "
                            "Choose Feed or Reels, not both."
                        )
                        % {"account": account.account_name}
                    }
                },
                status=400,
            )
    return None


def _validate_image_count(request, post, workspace, action):
    """Refuse to schedule more photos than a selected platform's feed post carries."""
    if action not in _PUBLISH_COMMITTING_ACTIONS:
        return None
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    accounts = [
        account
        for account in SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace)
        if max_images_per_post(account.platform) is not None
    ]
    if not accounts:
        return None
    image_count = 0 if post._state.adding else post_image_count(post)
    pending_ids = _pending_media_ids(request, post, workspace)
    if pending_ids:
        image_count += (
            MediaAsset.objects.filter(id__in=pending_ids, workspace=workspace, media_type="image")
            .exclude(file="")
            .count()
        )
    targets = _selected_targets(request, post, accounts)
    for account in accounts:
        for fmt in targets[str(account.id)]:
            reason = image_count_blocker(account, image_count=image_count, format=fmt)
            if reason:
                return JsonResponse({"errors": {"media": reason}}, status=400)
    return None


def _validate_required_media(request, post, workspace, action):
    """Refuse to schedule a post without the media a selected platform needs.

    TikTok and YouTube publish only video; Instagram and Pinterest need an
    image or a video. Their providers refuse anything else, so such a post can
    only fail — usually after its other platforms have gone out, leaving it
    half-published for the user to repair by hand.
    """
    if action not in _PUBLISH_COMMITTING_ACTIONS:
        return None
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    if not selected_ids:
        return None

    accounts = list(SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace))
    # Skip the media lookup when no selected platform cares about it.
    if not any(requires_media(account.platform) for account in accounts):
        return None
    media_types = _composer_media_types(request, post, workspace)
    blocked = [account for account in accounts if media_blocker(account, media_types=media_types)]
    if not blocked:
        return None

    # Worded apart from media_blocker's because this form can also be fixed by
    # deselecting the account, and it names every platform at once.
    video_only = sorted({a.get_platform_display() for a in blocked if is_video_only(a.platform)})
    any_media = sorted({a.get_platform_display() for a in blocked if not is_video_only(a.platform)})
    sentences = []
    if video_only:
        sentences.append(
            ngettext(
                "%(platforms)s can only publish videos.",
                "%(platforms)s can only publish videos.",
                len(video_only),
            )
            % {"platforms": get_text_list(video_only, _("and"))}
        )
    if any_media:
        sentences.append(
            ngettext(
                "%(platforms)s needs an image or a video.",
                "%(platforms)s need an image or a video.",
                len(any_media),
            )
            % {"platforms": get_text_list(any_media, _("and"))}
        )
    # A video satisfies every platform on the list, so ask for one when any
    # video-only platform is among them. Whole sentences rather than a slot for
    # the media kind, so a translation can reshape the clause around it.
    if video_only:
        sentences.append(
            ngettext(
                "Add a video to this post, or deselect that account.",
                "Add a video to this post, or deselect those accounts.",
                len(blocked),
            )
        )
    else:
        sentences.append(
            ngettext(
                "Add an image or a video to this post, or deselect that account.",
                "Add an image or a video to this post, or deselect those accounts.",
                len(blocked),
            )
        )
    return JsonResponse({"errors": {"media": " ".join(sentences)}}, status=400)


def _save_version(post, user):
    """Create a PostVersion snapshot."""
    from apps.social_accounts.youtube_compliance import authored_youtube_settings

    version_number = (post.versions.count()) + 1
    snapshot = {
        "title": post.title,
        "caption": post.caption,
        "first_comment": post.first_comment,
        "internal_notes": post.internal_notes,
        "tags": post.tags,
        "status": post.status,
        "scheduled_at": post.scheduled_at.isoformat() if post.scheduled_at else None,
        "platform_posts": [
            {
                "social_account_id": str(pp.social_account_id),
                "platform": pp.social_account.platform,
                "format": pp.format,
                "title_override": pp.platform_specific_title,
                "caption_override": pp.platform_specific_caption,
                "first_comment_override": pp.platform_specific_first_comment,
                "platform_extra": authored_youtube_settings(pp.platform_extra)
                if pp.social_account.platform == "youtube"
                else pp.platform_extra or {},
            }
            for pp in post.platform_posts.select_related("social_account")
        ],
        "media": [
            {
                "media_asset_id": str(pm.media_asset_id),
                "position": pm.position,
                "alt_text": pm.alt_text,
            }
            for pm in post.media_attachments.all()
        ],
    }
    PostVersion.objects.create(
        post=post,
        version_number=version_number,
        snapshot=snapshot,
        created_by=user,
    )


def _resolve_queues_for_post(queue_id, workspace, post_data):
    """Resolve the Queues to add a post to.

    Returns a list of Queue objects - one per selected social account when
    no explicit ``queue_id`` is supplied by the composer form. If an explicit
    queue_id is provided it is used exclusively.
    """
    from apps.calendar.models import Queue

    if queue_id:
        q = Queue.objects.filter(id=queue_id, workspace=workspace, is_active=True).first()
        return [q] if q else []

    account_ids = _parse_selected_account_ids(post_data.get("selected_accounts", ""))
    if not account_ids:
        return []

    queues = list(
        Queue.objects.filter(workspace=workspace, is_active=True, social_account_id__in=account_ids).order_by(
            "created_at"
        )
    )
    # De-duplicate by social_account (one queue per account)
    seen = set()
    unique = []
    for q in queues:
        if q.social_account_id in seen:
            continue
        seen.add(q.social_account_id)
        unique.append(q)
    return unique


def _resolve_template_data(template_id, workspace, variation=None):
    """Resolve a ?template= value into a template_data dict.

    Accepts either the numeric id of an ``IdeaTemplate`` (the shelf the
    operator keeps, shown in the create page; ``variation`` picks which
    version of its text, and the use is counted for the popular ones) or a
    UUID for a saved PostTemplate row. Returns ``None`` if the value is
    missing, malformed, or does not match any template.
    """
    if not template_id:
        return None
    # Built-in templates use numeric IDs.
    try:
        numeric_id = int(template_id)
    except (TypeError, ValueError):
        numeric_id = None
    if numeric_id is not None:
        idea_template = templates_for(workspace).filter(id=numeric_id).prefetch_related("variations").first()
        if idea_template is None:
            return None
        record_template_use(workspace, idea_template)
        return {
            "caption": template_text(idea_template, variation),
            "tags": [],
            "networks": idea_template.networks,
            "network_formats": idea_template.network_formats,
        }
    # Fall back to PostTemplate UUID lookup.
    try:
        tpl = PostTemplate.objects.get(id=template_id, workspace=workspace)
    except (PostTemplate.DoesNotExist, ValidationError):
        return None
    return tpl.template_data


def _automation_targets(platform_posts):
    """Where the composer's "set up flow" button of each account leads.

    The flow builder is reached per *platform post*, which only exists once
    the post has been saved at least once. Keyed "<account>:<format>" so the
    page can look one up from the account card the author is looking at; an
    account with no entry shows the "save first" state rather than a dead
    link. The flow's status rides along so the button can read "Edit flow ·
    Running" without a second query per account.
    """
    from apps.automations.models import PostAutomation

    platform_posts = list(platform_posts)
    flows = {}
    if platform_posts:
        flows = {
            flow.platform_post_id: flow
            for flow in PostAutomation.objects.filter(platform_post__in=[pp.id for pp in platform_posts])
        }
    targets = {}
    for pp in platform_posts:
        # A Story has no comment thread to automate.
        if pp.is_story:
            continue
        flow = flows.get(pp.id)
        targets[f"{pp.social_account_id}:{pp.format}"] = {
            "platform_post_id": str(pp.id),
            "status": flow.status if flow else "",
            "status_label": flow.get_status_display() if flow else "",
            "has_flow": flow is not None,
        }
    return targets


@login_required
@require_permission("create_posts")
@require_GET
def automation_targets(request, workspace_id, post_id):
    """The automation targets of a saved post, as JSON.

    Asked by the composer after each save that stays on the page: the
    platform posts are created by that save, so the targets rendered with
    the page are out of date until then.
    """
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    return JsonResponse(_automation_targets(post.platform_posts.all()))


@login_required
@require_permission("create_posts")
def compose(request, workspace_id, post_id=None):
    """Render the full-page composer for creating or editing a post."""
    workspace = _get_workspace(request, workspace_id)

    # ?account= scopes the composer to one connected account (calendar links).
    # Validate it up front: a malformed value must behave as "unscoped" rather
    # than leak into UUID lookups (ValidationError → 500) or the hidden
    # account_scope input the save endpoints trust.
    account_filter = request.GET.get("account", "").strip()
    if account_filter and not _is_valid_uuid(account_filter):
        account_filter = ""

    # Load existing post or prepare a blank one
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        # Enforce edit permissions: authors can edit their own posts,
        # but editing another user's post requires edit_others_posts.
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if post.author != request.user and not perms.get("edit_others_posts", False):
            raise PermissionDenied("You do not have permission to edit this post.")
        form = PostForm(instance=post)
        # Prefer a committed schedule; fall back to a draft-stage proposal so
        # the Schedule Post panel shows whichever time the post carries.
        prefill_dt = post.scheduled_at or post.proposed_publish_at
        if prefill_dt:
            import zoneinfo

            tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
            local_dt = prefill_dt.astimezone(tz)
            form.initial["scheduled_date"] = local_dt.strftime("%Y-%m-%d")
            form.initial["scheduled_time"] = local_dt.strftime("%H:%M")
        elif post.status == PlatformPost.Status.DRAFT:
            # A draft made outside the calendar (Radar offer, idea, task) has no
            # day yet; saving it with the panel's default gives it one.
            form.initial.update(_default_schedule_initial(workspace))
        # One fetch serves selected ids, extras, and the status checks below.
        platform_post_list = list(post.platform_posts.select_related("social_account"))
        selected_formats: dict[str, list[str]] = {}
        format_extras: dict[str, dict] = {}
        format_media: dict[str, dict] = {}
        # Matched strictly: the scoped form renders and selects only this account,
        # so an unmatched ?account= must select nothing. The read-only gate below
        # reuses the same match but falls back to every child — see _gated_children.
        scoped_platform_posts = _match_scope(platform_post_list, account_filter)
        selected_account_ids = list(dict.fromkeys(pp.social_account_id for pp in scoped_platform_posts))
        media_attachments = post.media_attachments.select_related("media_asset").all()
        # Account-level settings (overrides, YouTube, Pinterest, TikTok, place)
        # are written to every format of the account alike; the default row,
        # when there is one, is the canonical copy.
        platform_extras = {}
        for pp in sorted(platform_post_list, key=lambda p: p.format != PlatformPost.Format.DEFAULT):
            platform_extras.setdefault(str(pp.social_account_id), pp.platform_extra or {})
        replacements = format_media_replacements(scoped_platform_posts)
        for pp in scoped_platform_posts:
            for source_id, asset in replacements.get(pp.pk, {}).items():
                format_media.setdefault(f"{pp.social_account_id}:{pp.format}", {})[str(source_id)] = {
                    "id": str(asset.id),
                    "url": asset.file.url,
                    "width": asset.width,
                    "height": asset.height,
                }
        for pp in scoped_platform_posts:
            selected_formats.setdefault(str(pp.social_account_id), []).append(pp.format)
            extra = pp.platform_extra or {}
            if pp.is_story and is_notify_delivery(extra):
                format_extras.setdefault(str(pp.social_account_id), {})["story_delivery"] = NOTIFY_DELIVERY
            if pp.format == PlatformPost.Format.PROFILE_PHOTO and extra.get("photo_category"):
                format_extras.setdefault(str(pp.social_account_id), {})["photo_category"] = extra["photo_category"]
        template_data = None
    else:
        post = None
        # Pre-fill scheduled date/time from query params (e.g. when coming from calendar "+" CTA)
        initial = {}
        qs_date = request.GET.get("scheduled_date")
        qs_time = request.GET.get("scheduled_time")
        if qs_date:
            with contextlib.suppress(ValueError):
                initial["scheduled_date"] = datetime.strptime(qs_date, "%Y-%m-%d").date().isoformat()
        if qs_time:
            parsed_time = None
            for fmt in ("%H:%M", "%H:%M:%S"):
                try:
                    parsed_time = datetime.strptime(qs_time, fmt).time()
                    break
                except ValueError:
                    continue
            if parsed_time is not None:
                initial["scheduled_time"] = parsed_time.strftime("%H:%M")
        if "scheduled_date" not in initial:
            initial.update(_default_schedule_initial(workspace))
        elif "scheduled_time" not in initial:
            initial["scheduled_time"] = DEFAULT_POST_TIME.strftime("%H:%M")
        # Resolve ?template=<id> into template_data (supports both built-in int IDs
        # and PostTemplate UUIDs). Seed caption so it pre-fills the form.
        template_data = _resolve_template_data(request.GET.get("template"), workspace, request.GET.get("variation"))
        if template_data and template_data.get("caption"):
            initial["caption"] = template_data["caption"]
        form = PostForm(initial=initial)
        platform_post_list = []
        scoped_platform_posts = []
        selected_account_ids = []
        selected_formats = {}
        format_extras = {}
        format_media = {}
        media_attachments = []
        platform_extras = {}

    # Clear any stale pending media from previous compose sessions.
    # Each compose page load starts fresh; the upload flow re-populates
    # the session as the user adds files.
    from apps.media_library.models import MediaAsset

    session_key = f"pending_media_{workspace.id}"
    request.session.pop(session_key, None)
    pending_assets = MediaAsset.objects.none()

    # Connected social accounts for this workspace
    social_accounts = (
        SocialAccount.objects.for_workspace(workspace.id)
        .publishable()
        .filter(
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        .order_by("platform", "account_name")
    )

    # When opening from calendar with a specific account, show only that account.
    if account_filter:
        social_accounts = social_accounts.filter(id=account_filter)
        if not post_id and social_accounts.exists():
            selected_account_ids = [account_filter]
    # A template of the shelf comes with the brand's accounts on the networks it suits already ticked.
    elif not post_id and template_data and template_data.get("networks"):
        platforms = platforms_for_template_networks(template_data["networks"])
        selected_account_ids = [str(account.id) for account in social_accounts if account.platform in platforms]
        # ...each on the formats the template names for its network (Feed / Story / Reels).
        selected_formats.update(
            template_account_formats(
                template_data.get("network_formats"),
                [account for account in social_accounts if str(account.id) in selected_account_ids],
            )
        )

    # Platform character limits for JS
    from apps.automations.matching import COMMENT_PLATFORMS

    char_limits = {}
    for acc in social_accounts:
        cfg = dict(acc.field_config)
        cfg["supports_first_comment"] = acc.supports_first_comment()
        # Whether a comment automation can run on this account at all. Decided
        # by platform only — the comment webhook and the comment-addressed send
        # endpoint exist for exactly these platforms. Publication status is a
        # separate question the flow builder answers, because a draft post can
        # be configured now and armed when it goes live.
        cfg["supports_automation"] = acc.platform in COMMENT_PLATFORMS
        char_limits[str(acc.id)] = {
            "platform": acc.platform,
            # "Facebook", "Facebook Stories"…: with one account per platform
            # the channel name says which chip is which.
            "format_labels": {option["format"]: option["label"] for option in acc.format_options()},
            "limit": acc.char_limit,
            # Each of these costs two characters once the provider escapes it,
            # so the live counter can charge for them as the user types.
            "escaped_chars": acc.escaped_chars,
            "name": acc.account_name or acc.account_handle,
            "youtube_url": f"https://www.youtube.com/channel/{acc.account_platform_id}"
            if acc.platform == "youtube"
            else "",
            **cfg,
        }

    automation_targets = _automation_targets(platform_post_list)

    # Workspace defaults
    default_first_comment = workspace.default_first_comment
    default_hashtags = workspace.default_hashtags

    # Categories for dropdown
    categories = ContentCategory.objects.for_workspace(workspace.id)

    # Queues for "Add to Queue" action
    from apps.calendar.models import Queue

    queues = (
        Queue.objects.for_workspace(workspace.id).filter(is_active=True).select_related("social_account", "category")
    )

    # Permissions for action buttons
    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    can_publish = perms.get("publish_directly", False)
    can_approve = perms.get("approve_posts", False)

    # "Read-only" describes what the composer is actually *showing*. Opened
    # scoped to one account (``?account=``) only that child's status counts, so
    # a failed channel on a partially-published post stays workable; unscoped,
    # the aggregate decides. Clone is the escape hatch for the published ones.
    # _gated_children, not the strict match: an ?account= that names no row on
    # this post falls back to every child rather than reading as "nothing here".
    post_is_readonly = post is not None and _children_are_readonly(_gated_children(platform_post_list, account_filter))

    # Scoped away from the live channels, the composer would otherwise give no
    # sign they exist — and the caption being edited is shared with them. Name
    # them so the edit isn't made in ignorance of an already-published channel.
    scoped_ids = {pp.id for pp in scoped_platform_posts}
    live_siblings = (
        [pp for pp in platform_post_list if pp.id not in scoped_ids and pp.status in PlatformPost.PROTECTED_STATUSES]
        if not post_is_readonly
        else []
    )

    # Approval workflow context
    workflow_mode = workspace.approval_workflow_mode
    # A read-only post is done with the workflow — neither button applies.
    show_resubmit_button = not post_is_readonly and any(
        pp.status in ("changes_requested", "rejected", "approved") for pp in platform_post_list
    )
    # Fresh drafts get "Submit for Approval"; posts already in the workflow
    # (changes-requested / rejected / approved-but-edited) get "Resubmit" instead.
    show_submit_button = workflow_mode != "none" and not show_resubmit_button and not post_is_readonly
    # Once the post is committed to publishing, the Schedule Post panel re-times
    # a live schedule; while still a draft it captures a *proposed* time on save.
    # Mirror _capture_proposed_publish_at's guard exactly (scheduled_at OR a
    # committed child) so the hint, the JS schedule-arming, and the save path
    # never disagree.
    post_is_scheduled = post is not None and (
        post.scheduled_at is not None
        or any(pp.status in ("scheduled", "publishing", "published") for pp in platform_post_list)
    )

    # Approval history and comments for existing posts
    approval_history = []
    post_comments = []
    latest_feedback = None
    if post:
        from apps.approvals.models import ApprovalAction

        # Show recent history (bounded) — a heavily-cycled post can accumulate
        # unboundedly many actions; 50 is plenty for the timeline.
        approval_history = list(
            ApprovalAction.objects.filter(post=post).select_related("user").order_by("-created_at")[:50]
        )
        # Most recent reviewer feedback to surface in the edit banner.
        latest_feedback = next(
            (a for a in approval_history if a.action in ("changes_requested", "rejected") and a.comment),
            None,
        )
        from apps.approvals.comments import get_comments_for_post

        post_comments = get_comments_for_post(post, request.user)

    # Workspace tags for the tag dropdown
    all_tags = Tag.objects.for_workspace(workspace.id)

    # Failed platform posts with an error message — shown as a banner so the
    # user can see why a publish failed before retrying.
    failed_platform_posts = [
        pp for pp in platform_post_list if pp.status == PlatformPost.Status.FAILED and pp.publish_error
    ]

    # A first comment that failed leaves the post itself published, so it never
    # shows up in the banner above — without this the post looks fully
    # successful while the comment is silently missing.
    failed_first_comments = [
        pp for pp in platform_post_list if pp.first_comment_status == PlatformPost.FirstCommentStatus.FAILED
    ]

    # Build media_items for the initial preview render
    media_items = [_preview_media_item(att.media_asset) for att in media_attachments]
    if not media_items:
        media_items = [_preview_media_item(asset) for asset in pending_assets]

    # Resolve thumbnail/cover image URLs for per-account assets already saved
    thumb_ids = [
        extra.get("thumbnail_asset_id") for extra in platform_extras.values() if extra.get("thumbnail_asset_id")
    ]
    cover_ids = [
        extra.get("cover_image_asset_id") for extra in platform_extras.values() if extra.get("cover_image_asset_id")
    ]
    all_asset_ids = [aid for aid in thumb_ids + cover_ids if aid]
    asset_url_map = {}
    if all_asset_ids:
        for asset in MediaAsset.objects.filter(id__in=all_asset_ids, workspace=workspace):
            url = ""
            if asset.thumbnail:
                url = asset.thumbnail.url
            elif asset.file:
                url = asset.file.url
            asset_url_map[str(asset.id)] = url
    for _acc_id, extra in platform_extras.items():
        tid = extra.get("thumbnail_asset_id")
        if tid and tid in asset_url_map:
            extra["thumbnail_url"] = asset_url_map[tid]
        cid = extra.get("cover_image_asset_id")
        if cid and cid in asset_url_map:
            extra["cover_image_url"] = asset_url_map[cid]

    context = {
        "workspace": workspace,
        "post": post,
        "form": form,
        "social_accounts": social_accounts,
        # Every channel on offer, connected or not; not while scoped to one account.
        "unconnected_channels": (
            []
            if account_filter
            else PlatformVisibility.unconnected_choices(
                SocialAccount.objects.for_workspace(workspace.id)
                .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
                .values_list("platform", flat=True)
            )
        ),
        "can_connect_channels": bool(
            request.workspace_membership
            and request.workspace_membership.effective_permissions.get("manage_social_accounts")
        ),
        "selected_account_ids": [str(aid) for aid in selected_account_ids],
        "selected_formats": selected_formats,
        "format_extras": format_extras,
        # {"<account>:<format>": {source asset id: {id, url, width, height}}}:
        # the crops a format publishes instead of the attachment.
        "format_media": format_media,
        "platform_extras": platform_extras,
        "media_attachments": media_attachments,
        "media_items": media_items,
        **_facebook_grid_context(media_items),
        "char_limits": char_limits,
        "automation_targets": automation_targets,
        "default_first_comment": default_first_comment,
        "default_hashtags": json.dumps(default_hashtags),
        "can_publish": can_publish,
        "can_approve": can_approve,
        "is_edit": post is not None,
        **_network_delete_context(_gated_children(platform_post_list, account_filter) if post else []),
        "post_is_scheduled": post_is_scheduled,
        "post_is_readonly": post_is_readonly,
        "live_siblings": live_siblings,
        "categories": categories,
        "queues": queues,
        "template_data_json": json.dumps(template_data) if template_data else "null",
        "workflow_mode": workflow_mode,
        "show_submit_button": show_submit_button,
        "show_resubmit_button": show_resubmit_button,
        "approval_history": approval_history,
        "latest_feedback": latest_feedback,
        "post_comments": post_comments,
        "pending_assets": pending_assets,
        "all_tags": all_tags,
        # name -> kind, so the composer can mark which tags also get published.
        "tag_kinds_json": json.dumps({t.name: t.kind for t in all_tags}),
        "tag_groups": tag_groups_payload(workspace.id),
        # When opened scoped to one account (?account=), the form only renders
        # that account — the save endpoints use this to leave siblings alone.
        "account_scope": account_filter if (post_id and account_filter) else "",
        "failed_platform_posts": failed_platform_posts,
        "failed_first_comments": failed_first_comments,
        "unsplash_enabled": bool(settings.UNSPLASH_ACCESS_KEY),
        # Serialised into the page for the caption emoji picker and the
        # Facebook feeling selector; both are static tables, not queries.
        "emoji_categories": emoji_catalogue(),
        "facebook_feelings": facebook_feeling_choices(),
    }
    return render(request, "composer/compose.html", context)


def _transition_post_children(post, target, *, allow_via_draft=True, only=None):
    """Transition every (or selected) PlatformPost child of *post* to *target*.

    Returns ``(moved, skipped)`` lists of PlatformPost instances. Children
    already in the target state are left alone (counted as moved). For states
    that don't allow a direct transition, an intermediate hop through
    ``draft`` is attempted when ``allow_via_draft`` is True.

    ``only`` may be an iterable of PlatformPost IDs to restrict the operation
    to a subset of children.
    """
    children = post.platform_posts.all()
    if only is not None:
        only_ids = {str(x) for x in only}
        children = [pp for pp in children if str(pp.id) in only_ids]
    moved, skipped = [], []
    for pp in children:
        if pp.status == target:
            moved.append(pp)
            continue
        try:
            if pp.can_transition_to(target):
                pp.transition_to(target)
            elif allow_via_draft and pp.can_transition_to("draft") and target != "draft":
                pp.transition_to("draft")
                if pp.can_transition_to(target):
                    pp.transition_to(target)
                else:
                    skipped.append(pp)
                    continue
            else:
                skipped.append(pp)
                continue
            pp.save(update_fields=[*PlatformPost.TRANSITION_FIELDS, "updated_at"])
            moved.append(pp)
        except ValueError:
            skipped.append(pp)
    return moved, skipped


# ``save_post`` actions that hand rows to the publisher on a date. ``publish_now``
# is not among them: it takes ``publish_directly`` whatever the workspace asks.
_SCHEDULING_ACTIONS = ("schedule", "add_to_queue", "add_to_queue_priority")


def _reviewed_platform_post_ids(request, post):
    """Ids of the rows this request may schedule on a reviewer's word.

    Approved rows, and rows already scheduled (moving their time is not a new
    decision to publish), inside the composer's ``account_scope``.
    """
    rows = post.platform_posts.filter(status__in=("approved", "scheduled"))
    scope = _get_account_scope(request)
    if scope:
        rows = rows.filter(social_account_id=scope)
    return {str(pk) for pk in rows.values_list("id", flat=True)}


def _queues_for_schedulable(post, queues, schedulable_ids):
    """``queues`` without those of accounts whose rows this save may not schedule."""
    if schedulable_ids is None:
        return queues
    accounts = set(post.platform_posts.filter(id__in=schedulable_ids).values_list("social_account_id", flat=True))
    return [queue for queue in queues if queue.social_account_id in accounts]


def _rows_to_schedule(request, post, orig_content, schedulable_ids):
    """Apply the review rules to a save; returns the rows a scheduling action may move.

    Content a reviewer cleared and this save changed goes back to review
    first, so a schedule in the same save can't carry the edit out. What is
    returned suits ``_transition_post_children(only=...)``: ``None`` for every
    row, else the ids left once the rows sent back and the rows outside
    ``schedulable_ids`` (``None`` = no limit) and the composer's scope are out.
    """
    scoped_ids = _scoped_platform_post_ids(request, post)
    sent_back: set[str] = set()
    if orig_content is not None and reviewed_content_changed(orig_content, reviewed_content_snapshot(post)):
        sent_back = {str(pp.id) for pp in _send_back_for_review(request, post)}
    if not sent_back and schedulable_ids is None:
        return scoped_ids
    candidates = scoped_ids if scoped_ids is not None else [pp.id for pp in post.platform_posts.all()]
    return [
        pid
        for pid in candidates
        if str(pid) not in sent_back and (schedulable_ids is None or str(pid) in schedulable_ids)
    ]


def _platform_status_map(post):
    """Return ``{platform_post_id: status}`` for HTMX response headers."""
    return {str(pp.id): pp.status for pp in post.platform_posts.all()}


# The time a post gets when only its day is known (a day of the calendar).
DEFAULT_POST_TIME = time(12, 0)


def _default_schedule_initial(workspace):
    """The Schedule panel's date and time for a post that has none: today, at noon.

    The draft is saved with the panel's day, so it shows on the calendar instead
    of having no day at all.
    """
    return {
        "scheduled_date": _workspace_now(workspace).date().isoformat(),
        "scheduled_time": DEFAULT_POST_TIME.strftime("%H:%M"),
    }


def _combine_schedule_dt(workspace, sched_date, sched_time):
    """Combine the composer's date + time inputs into a workspace-tz-aware datetime.

    Returns ``None`` unless both parts are present. Shared by the real
    schedule (``action='schedule'``) and the draft-stage proposed time so both
    interpret the same Schedule Post panel inputs identically.
    """
    if not (sched_date and sched_time):
        return None
    import zoneinfo

    tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    return datetime.combine(sched_date, sched_time).replace(tzinfo=tz)


def _proposed_from_inputs(workspace, date_str, time_str):
    """The Schedule panel's raw date and time strings as a proposed datetime, or None.

    A date without a (valid) time gets ``DEFAULT_POST_TIME``.
    """
    try:
        sched_date = date.fromisoformat((date_str or "").strip())
    except ValueError:
        return None
    try:
        sched_time = datetime.strptime((time_str or "").strip(), "%H:%M").time()
    except ValueError:
        sched_time = DEFAULT_POST_TIME
    return _combine_schedule_dt(workspace, sched_date, sched_time)


def _capture_proposed_publish_at(post, post_id, workspace, form, *, clear_when_blank=True):
    """Set ``post.proposed_publish_at`` from the composer's Schedule Post panel.

    Used by the draft-stage save actions (save draft, submit/resubmit for
    approval) so a proposed time entered in the panel is persisted and shown in
    the drafts/approval lists. Mutates ``post`` in place; the caller persists it.

    A no-op once the post is committed to publishing — ``post.scheduled_at`` is
    set or any child is scheduled/publishing/published — because then the
    date/time inputs reflect the live schedule (pre-filled from ``scheduled_at``)
    and must not be reinterpreted as a proposal.

    ``clear_when_blank`` (default True, for Save Draft where the panel *is* the
    proposed-time editor) clears the proposal when the panel is empty. The
    approval submit/resubmit actions pass False so routing a draft through
    approval with an untouched/absent panel can't silently wipe a proposal an
    agent set via the REST/MCP API.
    """
    already_scheduled = post.scheduled_at is not None or (
        bool(post_id) and post.platform_posts.filter(status__in=["scheduled", "publishing", "published"]).exists()
    )
    if already_scheduled:
        return
    sched_date = form.cleaned_data.get("scheduled_date")
    proposed = _combine_schedule_dt(
        workspace, sched_date, form.cleaned_data.get("scheduled_time") or (sched_date and DEFAULT_POST_TIME)
    )
    if proposed is None and not clear_when_blank:
        return
    post.proposed_publish_at = proposed


def _ordered_pending_ids(request, session_ids):
    """``session_ids`` in the order the composer is showing them.

    The session records uploads in arrival order, but the thumbnails are
    drag-sortable, so the page posts the order it is displaying in
    ``media_order``. Anything the page didn't name keeps its session order at
    the end: a stale field can reorder the carousel but never drop a file
    from it.
    """
    wanted = [value for value in request.POST.get("media_order", "").split(",") if value]
    if not wanted:
        return session_ids
    by_id = {str(asset_id): asset_id for asset_id in session_ids}
    ordered = [by_id.pop(value) for value in wanted if value in by_id]
    return ordered + list(by_id.values())


def _reorder_saved_media(request, post):
    """Apply the composer's ``media_order`` to an existing post's attachments.

    The field names ``PostMedia`` ids. Attachments it doesn't name keep their
    relative order after the named ones, so a reorder posted against a stale
    page can't strand an attachment the user added in another tab.
    """
    wanted = [value for value in request.POST.get("media_order", "").split(",") if value]
    if not wanted:
        return
    attachments = {str(att.id): att for att in post.media_attachments.all()}
    ordered = [attachments.pop(value) for value in wanted if value in attachments]
    ordered += list(attachments.values())
    changed = []
    for idx, attachment in enumerate(ordered):
        if attachment.position != idx:
            attachment.position = idx
            changed.append(attachment)
    if changed:
        PostMedia.objects.bulk_update(changed, ["position"])


def _attach_pending_media(request, post, workspace, session_key, *, pending_ids=None):
    """Move the session's pending uploads onto ``post``.

    All of them for a freshly created post; ``pending_ids`` (see
    ``_pending_media_ids``) names the ones a saved post claims, which go after
    its attachments and leave the rest of the session alone.

    Positions follow ``_ordered_pending_ids``, so the carousel publishes in
    the order the user arranged rather than the order they uploaded.
    """
    from apps.media_library.models import MediaAsset

    session_ids = request.session.get(session_key, [])
    claimed = session_ids if pending_ids is None else pending_ids
    if not claimed:
        return
    offset = 0 if pending_ids is None else post.media_attachments.count()
    for idx, asset_id in enumerate(_ordered_pending_ids(request, claimed)):
        try:
            asset = MediaAsset.objects.get(id=asset_id, workspace=workspace)
        except MediaAsset.DoesNotExist:
            continue
        PostMedia.objects.get_or_create(
            post=post,
            media_asset=asset,
            defaults={"position": offset + idx},
        )
    left = [asset_id for asset_id in session_ids if asset_id not in claimed]
    if left:
        request.session[session_key] = left
    else:
        del request.session[session_key]


@login_required
@require_permission("create_posts")
@require_POST
def save_post(request, workspace_id, post_id=None):
    """Save or update a post (draft, schedule, or publish action)."""
    workspace = _get_workspace(request, workspace_id)
    action = request.POST.get("action", "save_draft")

    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        _require_post_editor(request, post)
        # Reject before the form binds, so an already-published post is never
        # mutated in memory. Catches every action, including the ``save_draft``
        # default a POST with no ``action`` falls back to.
        readonly_error = _readonly_rejection(request, post)
        if readonly_error is not None:
            return readonly_error
        _orig_content = reviewed_content_snapshot(post)
        form = PostForm(request.POST, instance=post)
    else:
        _orig_content = None
        form = PostForm(request.POST)

    # A member who needs an approval to schedule only ever moves rows a reviewer
    # cleared: ``schedulable_ids`` names them, and is None when there is no limit.
    schedulable_ids = None
    if action in _SCHEDULING_ACTIONS and needs_approval_to_schedule(workspace, _member_permissions(request)):
        schedulable_ids = _reviewed_platform_post_ids(request, post) if post_id else set()
        if not schedulable_ids:
            return JsonResponse(
                {"errors": {"schedule": _("This post has to be approved before it is scheduled.")}}, status=403
            )

    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)

    post = form.save(commit=False)
    post.workspace = workspace
    if not post_id:
        post.author = request.user

    pinterest_board_error = _validate_pinterest_board_selection(request, post, workspace)
    if pinterest_board_error is not None:
        return pinterest_board_error

    media_error = (
        _validate_required_media(request, post, workspace, action)
        or _validate_formats(request, post, workspace, action)
        or _validate_image_count(request, post, workspace, action)
    )
    if media_error is not None:
        return media_error

    # Handle action — note that Post itself no longer carries an editorial
    # status: every transition below operates on the PlatformPost children,
    # which is why we sync those before/after running it.
    pending_target = None  # what to transition existing children to after sync
    initial_status = "draft"  # default status for newly created PlatformPosts

    if action == "schedule":
        aware_dt = _combine_schedule_dt(
            workspace,
            form.cleaned_data.get("scheduled_date"),
            form.cleaned_data.get("scheduled_time"),
        )
        if aware_dt:
            if aware_dt <= timezone.now():
                return JsonResponse(
                    {"errors": {"schedule": _("Scheduled time must be in the future.")}},
                    status=400,
                )
            post.scheduled_at = aware_dt
            # A committed schedule supersedes any draft-stage proposal.
            post.proposed_publish_at = None
            # Propagate the manually chosen time to every PlatformPost so all
            # selected platforms publish at the same moment.
            post._schedule_propagate_dt = aware_dt  # handled after post.save()
            pending_target = "scheduled"
            # An account added in this save has no approval yet.
            initial_status = "scheduled" if schedulable_ids is None else "draft"
        else:
            return JsonResponse({"errors": {"schedule": _("Date and time required.")}}, status=400)
    elif action == "publish_now":
        # Server-side permission check - only roles with publish_directly can bypass approval
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if not perms.get("publish_directly", False):
            raise PermissionDenied("You do not have permission to publish directly.")
        now_dt = timezone.now()
        post.scheduled_at = now_dt
        post.proposed_publish_at = None
        post._schedule_propagate_dt = now_dt  # handled after post.save()
        pending_target = "scheduled"
        initial_status = "scheduled"
    elif action == "add_to_queue":
        from django.db import transaction

        from apps.calendar.services import QueueFullError, add_to_queue

        queue_id = request.POST.get("queue_id")
        queues = _queues_for_schedulable(
            post, _resolve_queues_for_post(queue_id, workspace, request.POST), schedulable_ids
        )
        if not queues:
            return JsonResponse({"errors": {"queue": _("No active queue found for the selected channel.")}}, status=400)
        # Queueing assigns real per-platform slots below — drop any proposal.
        post.proposed_publish_at = None
        post.save()
        # Ensure PlatformPost rows exist for every selected account before the
        # queue service writes per-platform scheduled_at values.
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        queue_rows = _rows_to_schedule(request, post, _orig_content, schedulable_ids)
        # "Next Available" always places the post in the queue's soonest open
        # slot. It deliberately ignores the Schedule-panel date/time: those
        # inputs are prefilled from the post's own scheduled_at/proposed time
        # when editing, and treating them as a floor would push the post past
        # the true next slot.
        try:
            # One transaction across every queue: if a later queue is full, the
            # earlier queues' slot writes roll back instead of leaving a child
            # half-queued.
            with transaction.atomic():
                for q in queues:
                    add_to_queue(post, q)
                # Transition every child whose scheduled_at was filled in.
                _transition_post_children(post, "scheduled", only=queue_rows, allow_via_draft=schedulable_ids is None)
        except QueueFullError:
            return JsonResponse({"errors": {"queue": _QUEUE_FULL_MSG}}, status=400)
        _save_version(post, request.user)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "add_to_queue_priority":
        from django.db import transaction

        from apps.calendar.services import QueueFullError, add_to_queue

        queue_id = request.POST.get("queue_id")
        queues = _queues_for_schedulable(
            post, _resolve_queues_for_post(queue_id, workspace, request.POST), schedulable_ids
        )
        if not queues:
            return JsonResponse({"errors": {"queue": _("No active queue found for the selected channel.")}}, status=400)
        # Queueing assigns real per-platform slots below — drop any proposal.
        post.proposed_publish_at = None
        post.save()
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        queue_rows = _rows_to_schedule(request, post, _orig_content, schedulable_ids)
        try:
            # One transaction across every queue (see add_to_queue above).
            with transaction.atomic():
                for q in queues:
                    add_to_queue(post, q, priority=True)
                _transition_post_children(post, "scheduled", only=queue_rows, allow_via_draft=schedulable_ids is None)
        except QueueFullError:
            return JsonResponse({"errors": {"queue": _QUEUE_FULL_MSG}}, status=400)
        _save_version(post, request.user)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "submit_for_approval":
        # Save post first so it has a PK, then delegate to approval service
        _capture_proposed_publish_at(post, post_id, workspace, form, clear_when_blank=False)
        post.save()
        # Sync platform posts before submitting
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        _save_version(post, request.user)
        from apps.approvals.services import submit_for_review

        submit_for_review(post, request.user, workspace)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "resubmit_for_approval":
        # Resubmit after changes requested or rejection
        _capture_proposed_publish_at(post, post_id, workspace, form, clear_when_blank=False)
        post.save()
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        _save_version(post, request.user)
        from apps.approvals.services import resubmit_post

        resubmit_post(post, request.user, workspace)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "save_draft":
        # The Schedule Post panel doubles as the proposed-time picker for a
        # draft; capture it (or clear it when blank) before the save below.
        _capture_proposed_publish_at(post, post_id, workspace, form)
    # Fall through (save_draft / unknown action): persist below; existing
    # children are left as-is, new children default to draft via initial_status.

    post.save()

    session_key = f"pending_media_{workspace.id}"
    if not post_id:
        _attach_pending_media(request, post, workspace, session_key)
    else:
        _attach_pending_media(
            request, post, workspace, session_key, pending_ids=_pending_media_ids(request, post, workspace)
        )
        _reorder_saved_media(request, post)

    # Sync any new tags to the Tag model
    _sync_tags_to_model(workspace, post.tags)

    # Handle recurring post creation
    make_recurring = request.POST.get("make_recurring")
    if make_recurring and action == "schedule" and post.scheduled_at:
        from apps.calendar.models import RecurrenceRule

        frequency = request.POST.get("recurrence_frequency", "weekly")
        interval_str = request.POST.get("recurrence_interval", "1")
        end_date_str = request.POST.get("recurrence_end_date", "")
        try:
            interval_val = int(interval_str)
        except (ValueError, TypeError):
            interval_val = 1
        end_date_val = None
        if end_date_str:
            try:
                from datetime import date as date_cls

                end_date_val = date_cls.fromisoformat(end_date_str)
            except (ValueError, TypeError):
                pass
        RecurrenceRule.objects.update_or_create(
            post=post,
            defaults={
                "frequency": frequency,
                "interval": interval_val,
                "end_date": end_date_val,
                "is_active": True,
            },
        )

    # Sync platform posts (newly-created rows inherit ``initial_status`` for
    # this action — e.g. "scheduled" for schedule/publish_now, "draft" for
    # save_draft).
    _sync_platform_posts(request, post, workspace, initial_status=initial_status)

    # Propagate manually-chosen schedule/publish_now datetimes to every
    # PlatformPost now that they exist — except published/publishing rows
    # (their schedule is history) and, in scoped mode, siblings outside the
    # ``?account=`` scope.
    scoped_ids = _scoped_platform_post_ids(request, post)
    propagate_dt = getattr(post, "_schedule_propagate_dt", None)
    if propagate_dt is not None:
        propagate_qs = post.platform_posts.exclude(status__in=PlatformPost.PROTECTED_STATUSES)
        if scoped_ids is not None:
            propagate_qs = propagate_qs.filter(id__in=scoped_ids)
        if schedulable_ids is not None:
            propagate_qs = propagate_qs.filter(id__in=schedulable_ids)
        propagate_qs.update(scheduled_at=propagate_dt)

    # Option A: editing reviewed content sends it back for re-approval so edits
    # can't publish un-reviewed. Run this BEFORE applying any publish target —
    # otherwise a schedule/publish_now in the *same* save would push the
    # just-edited (no-longer-approved) content straight to scheduled.
    target_only = _rows_to_schedule(request, post, _orig_content, schedulable_ids)

    # Move existing children to the requested target state (no-op for save_draft —
    # children that are already mid-workflow stay put). Children just reverted for
    # re-review are excluded so a same-save publish action can't drag them back
    # out of review.
    if pending_target:
        _transition_post_children(post, pending_target, only=target_only, allow_via_draft=schedulable_ids is None)

    # Save version
    _save_version(post, request.user)

    # Return appropriate response
    if request.htmx:
        return HttpResponse(
            status=204,
            headers={
                "HX-Trigger": json.dumps(
                    {
                        "postSaved": {
                            "postId": str(post.id),
                            "status": post.status,
                        }
                    }
                ),
                "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
            },
        )

    return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)


@login_required
@require_permission("create_posts")
@require_POST
def transition_platform_post(request, workspace_id, post_id, platform_post_id):
    """Transition a single PlatformPost to a target editorial status.

    Used by the composer's per-account chip menu so the user can take a single
    social account in/out of draft, schedule, etc. without affecting siblings.
    Permission rules mirror save_post: ``approve_posts`` is required for any
    approval-stage target; ``publish_directly`` is required for scheduled.
    """
    workspace = _get_workspace(request, workspace_id)
    pp = get_object_or_404(
        PlatformPost.objects.select_related("post", "social_account"),
        id=platform_post_id,
        post_id=post_id,
        post__workspace=workspace,
    )
    target = (request.POST.get("target_status") or "").strip()
    if not target:
        return JsonResponse({"error": "target_status required"}, status=400)

    perms = _member_permissions(request)
    approval_states = {"approved", "pending_review", "pending_client", "changes_requested", "rejected"}
    if target in ("scheduled", "publishing") and not perms.get("publish_directly", False):
        raise PermissionDenied("You do not have permission to schedule this post.")
    if target in approval_states and target != "pending_review":
        if not perms.get("approve_posts", False):
            raise PermissionDenied("You do not have permission to make approval decisions.")
    else:
        # Anything but a reviewer's decision (back to draft, on hold, to
        # review, to the publisher) is a change to the post itself.
        _require_post_editor(request, pp.post)

    if pp.status == target:
        return JsonResponse({"ok": True, "status": pp.status, "noop": True})

    if target in ("scheduled", "publishing"):
        # save_post's own checks never see this endpoint: it moves one row
        # straight to the publisher, so a failed image-only TikTok row could
        # otherwise be "retried" into the same failure.
        try:
            require_publishable(pp)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

    try:
        pp.transition_to(target)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    pp.save(update_fields=[*PlatformPost.TRANSITION_FIELDS, "updated_at"])
    # Committing a child to publishing obsoletes any draft-stage proposal.
    # Clear it directly rather than via sync_post_scheduled_at: this view sets
    # ``scheduled`` WITHOUT a ``scheduled_at``, and the publisher relies on the
    # ``Coalesce(scheduled_at, post__scheduled_at)`` fallback — recomputing the
    # Post.scheduled_at aggregate here could strand a post that depends on it.
    if target in ("scheduled", "publishing", "published") and pp.post.proposed_publish_at is not None:
        pp.post.proposed_publish_at = None
        pp.post.save(update_fields=["proposed_publish_at", "updated_at"])
    return JsonResponse({"ok": True, "status": pp.status, "platform_post_id": str(pp.id)})


def _workspace_now(workspace):
    """The current time in the workspace's timezone, as the user reads the clock."""
    import zoneinfo

    try:
        return timezone.now().astimezone(zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC"))
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return timezone.now()


@login_required
@require_permission("create_posts")
@require_POST
def autosave(request, workspace_id, post_id=None):
    """Auto-save endpoint called every 30 seconds via HTMX.

    On first save for a new post (no post_id), creates the draft and returns
    an HX-Trigger with the new post ID so the client can switch the autosave
    URL to the edit endpoint, preventing duplicate drafts on subsequent ticks.
    """
    workspace = _get_workspace(request, workspace_id)

    is_new = False
    orig_content = None
    post = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
    else:
        # A previous autosave of this composer session may already have created
        # the draft; the client then names it in the body.
        client_post_id = request.POST.get("_autosave_post_id", "").strip()
        if _is_valid_uuid(client_post_id):
            post = Post.objects.filter(id=client_post_id, workspace=workspace).first()

    if post is None:
        post = Post(workspace=workspace, author=request.user)
        is_new = True
    else:
        # The same rules for an existing post, whichever way its id arrived.
        _require_post_editor(request, post)
        # A published post is read-only: silently stop saving rather than
        # overwriting live content every 30 seconds. This returns 200 with the
        # indicator markup on purpose — the response swaps into #autosave-status,
        # so a 4xx here would fire the global error toast on every tick.
        if _is_readonly_write(request, post):
            return HttpResponse(
                format_html('<span class="text-xs text-gray-400">{}</span>', _("Read-only — not saved"))
            )
        orig_content = reviewed_content_snapshot(post)

    post.title = request.POST.get("title", "")
    post.caption = request.POST.get("caption", "")
    post.first_comment = request.POST.get("first_comment", "")
    # The composer has no notes field; notes written through the API or MCP
    # are kept when the post is saved here.
    if "internal_notes" in request.POST:
        post.internal_notes = request.POST["internal_notes"]

    post.tags = parse_and_truncate_tag_string(request.POST.get("tags", ""))

    # The day picked in the Schedule panel (the calendar day the post was started
    # on) is the draft's proposed day from the first save, so the draft shows on
    # that day of the calendar; a scheduled post keeps its own time.
    if post.scheduled_at is None and not (
        post.pk and post.platform_posts.filter(status__in=["scheduled", "publishing", "published"]).exists()
    ):
        proposed = _proposed_from_inputs(
            workspace, request.POST.get("scheduled_date"), request.POST.get("scheduled_time")
        )
        if proposed is not None:
            post.proposed_publish_at = proposed

    # Opening the composer and walking away must not leave an empty draft
    # behind: a new post is only created once there is something to keep.
    if is_new and not (
        post.title.strip()
        or post.caption.strip()
        or post.first_comment.strip()
        or post.internal_notes.strip()
        or post.tags
        or _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
        or request.session.get(f"pending_media_{workspace.id}")
    ):
        return HttpResponse(format_html('<span class="text-xs text-gray-400">{}</span>', _("Nothing to save yet")))

    post.save()

    session_key = f"pending_media_{workspace.id}"
    if is_new:
        _attach_pending_media(request, post, workspace, session_key)
    else:
        _attach_pending_media(
            request, post, workspace, session_key, pending_ids=_pending_media_ids(request, post, workspace)
        )
        _reorder_saved_media(request, post)

    # Sync platform selections
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    accounts = list(SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace))
    targets = _selected_targets(request, post, accounts)
    _remove_deselected_platform_posts(request, post, targets)
    for account in accounts:
        for fmt in targets[str(account.id)]:
            PlatformPost.objects.get_or_create(post=post, social_account=account, format=fmt)

    # Option A: an autosave that changed reviewed content reverts it to
    # pending_review so edited content can't publish without a fresh review.
    if orig_content is not None and reviewed_content_changed(orig_content, reviewed_content_snapshot(post)):
        _send_back_for_review(request, post)

    return HttpResponse(
        format_html(
            '<span class="text-xs text-gray-400">{}</span>',
            _("Saved %(time)s") % {"time": _workspace_now(workspace).strftime("%H:%M")},
        ),
        headers={"HX-Trigger": json.dumps({"autosaved": {"postId": str(post.id), "isNew": is_new}})},
    )


@login_required
@require_POST
def preview(request, workspace_id):
    """Live preview endpoint - renders platform-specific preview from form state.

    Called via HTMX with debounced POST from the composer.
    Stateless - no DB queries except social account lookup.
    """
    workspace = _get_workspace(request, workspace_id)
    title = request.POST.get("title", "")
    caption = request.POST.get("caption", "")
    first_comment = request.POST.get("first_comment", "")
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))

    # The published tags ride along at the end of the caption, so the preview
    # has to show them — and count them against the limit — the same way
    # PostTarget.effective_caption will when the post goes out.
    tag_suffix = Tag.publishable_suffix(workspace.id, parse_and_truncate_tag_string(request.POST.get("tags", "")))

    def _with_tags(text):
        if not tag_suffix:
            return text
        return f"{text}\n\n{tag_suffix}" if text.strip() else tag_suffix

    # Build preview data per platform
    previews = []
    if selected_ids:
        accounts = SocialAccount.objects.filter(
            id__in=selected_ids,
            workspace=workspace,
        ).order_by("platform")
        for account in accounts:
            options = {option["format"]: option for option in account.format_options()}
            field_config = account.field_config
            char_limit = account.char_limit
            slides = []
            # One slide per chosen format, each with that format's own
            # customization.
            for fmt in _selected_targets(request, None, [account])[str(account.id)]:
                effective_title = _target_field(request.POST, "override_title", account.id, fmt) or title
                effective_caption = _with_tags(
                    _target_field(request.POST, "override_caption", account.id, fmt) or caption
                )
                # Counted after escaping, so the badge matches what gets published.
                char_count = account.caption_wire_length(effective_caption)
                slides.append(
                    {
                        "account": account,
                        "format": fmt,
                        "label": options[fmt]["label"] if fmt in options else account.get_platform_display(),
                        "format_label": options[fmt]["short"] if fmt in options else PlatformPost.Format(fmt).label,
                        "title": effective_title,
                        "caption": effective_caption,
                        "first_comment": first_comment,
                        "char_count": char_count,
                        "char_limit": char_limit,
                        "is_over_limit": char_count > char_limit,
                        "truncated_caption": effective_caption[:char_limit]
                        if len(effective_caption) > char_limit
                        else effective_caption,
                        "needs_title": field_config["needs_title"],
                    }
                )
            previews.append({"account": account, "slides": slides})

    # Gather media for preview - check pending session media or post attachments
    from apps.media_library.models import MediaAsset

    media_items = []
    post_id_str = request.POST.get("_autosave_post_id", "")

    post_obj = None
    attachment_asset_ids = []
    if post_id_str:
        try:
            post_obj = Post.objects.get(id=post_id_str, workspace=workspace)
            for att in post_obj.media_attachments.select_related("media_asset").all():
                media_items.append(_preview_media_item(att.media_asset))
                attachment_asset_ids.append(att.media_asset_id)
        except Post.DoesNotExist:
            pass

    # A format with its own crops (a 9:16 copy for the Story) shows them on
    # its slide; every other slide shows the post's media.
    if post_obj is not None and previews:
        rows = list(post_obj.platform_posts.all())
        replacements = format_media_replacements(rows)
        row_for = {(pp.social_account_id, pp.format): pp.pk for pp in rows}
        for preview in previews:
            for slide in preview["slides"]:
                swaps = replacements.get(row_for.get((slide["account"].id, slide["format"])), {})
                if swaps:
                    slide["media_items"] = [
                        _preview_media_item(swaps[asset_id]) if asset_id in swaps else item
                        for asset_id, item in zip(attachment_asset_ids, media_items, strict=True)
                    ]

    if not media_items:
        # Check session pending media
        session_key = f"pending_media_{workspace.id}"
        pending_ids = request.session.get(session_key, [])
        if pending_ids:
            for asset in MediaAsset.objects.filter(id__in=pending_ids, workspace=workspace):
                media_items.append(_preview_media_item(asset))

    # The format strip under each card: the first media the slide shows, and
    # the frame the format fills (Story and Reels 9:16, a profile photo 1:1;
    # an ordinary post keeps the media's own shape, so it names none).
    for preview_entry in previews:
        for slide in preview_entry["slides"]:
            shown = slide.get("media_items") or media_items
            slide["thumb"] = shown[0] if shown else None
            slide["ratio"] = PREVIEW_FORMAT_RATIOS.get(slide["format"], "")

    return render(
        request,
        "composer/partials/preview_panel.html",
        {
            "previews": previews,
            "workspace": workspace,
            "media_items": media_items,
            **_facebook_grid_context(media_items),
        },
    )


@login_required
@require_GET
def media_picker(request, workspace_id, post_id=None, target=""):
    """Modal picker for selecting media from the library.

    Runs the library's own search/filter/sort/paginate pipeline
    (``media_library.views.filtered_assets``) and renders the library's own
    asset grid, so the picker shows the same media library the user already
    knows instead of a parallel one. ``picker_mode`` turns the shared partials
    from a manager into a selector: the cards carry a checkbox and no
    star/detail actions, and every HTMX URL points back here.

    ``target`` "idea" is the same window on the idea board: the selection goes
    back to the page instead of being attached to a post.

    Responds with the grid alone for HTMX requests driven by search, a filter
    pill, sort or pagination; the full modal body otherwise.
    """
    from apps.media_library.models import MediaFolder
    from apps.media_library.validators import get_accepted_file_types
    from apps.media_library.views import ASSETS_PER_PAGE, filtered_assets

    workspace = _get_workspace(request, workspace_id)

    post = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)

    qs, filters = filtered_assets(request, workspace)
    page = Paginator(qs, ASSETS_PER_PAGE).get_page(request.GET.get("page", 1))

    context = {
        "page": page,
        "workspace": workspace,
        "post": post,
        "picker_mode": True,
        "picker_target": target,
        "file_types": MediaAsset.MediaType.choices,
        "accepted_file_types": get_accepted_file_types(),
        **filters,
    }

    # The grid alone for search/filter/sort/paginate; the whole body on open.
    # Without the header check, typing in the search box would swap the modal's
    # own chrome (search field included) into the grid container.
    if request.headers.get("HX-Target") == "picker-asset-grid":
        return render(request, "media_library/_asset_grid.html", context)

    from apps.billing.storage_usage import storage_status

    context["folders"] = MediaFolder.objects.filter(
        workspace=workspace,
        parent_folder__isnull=True,
    ).prefetch_related("subfolders__subfolders")
    context["storage"] = storage_status(workspace)
    return render(request, "composer/partials/media_picker.html", context)


@login_required
@require_GET
def thumbnail_picker(request, workspace_id):
    """Modal picker for selecting an image asset as a YouTube thumbnail.

    Image-only, selection dispatches a client-side event; does not attach
    anything server-side.
    """
    workspace = _get_workspace(request, workspace_id)
    from apps.media_library.models import MediaAsset

    assets = (
        MediaAsset.objects.for_workspace(workspace.id)
        .filter(media_type=MediaAsset.MediaType.IMAGE)
        .order_by("-created_at")[:50]
    )
    return render(
        request,
        "composer/partials/thumbnail_picker.html",
        {"assets": assets, "workspace": workspace},
    )


@login_required
@require_permission("create_posts")
@require_permission("upload_media")
@require_POST
def thumbnail_upload(request, workspace_id):
    """Upload an image from the local machine to the media library and return
    its id + URL so the composer can wire it as a YouTube thumbnail.

    Image-only. Returns JSON with asset_id, url, filename.
    """
    workspace = _get_workspace(request, workspace_id)
    uploaded_file = request.FILES.get("file")

    if not uploaded_file:
        return JsonResponse({"error": _("No file provided")}, status=400)

    from apps.media_library.validators import image_storage_filename

    if not image_storage_filename(uploaded_file):
        return JsonResponse({"error": _("Only image files are allowed")}, status=400)

    try:
        asset = _create_idea_media_asset(workspace, request.user, uploaded_file)
    except ValidationError as exc:
        return _upload_rejected_response(exc)
    except (StorageQuotaExceededError, WorkspaceStorageExceededError) as exc:
        return _quota_error_response(exc)

    url = ""
    if asset.thumbnail:
        url = asset.thumbnail.url
    elif asset.file:
        url = asset.file.url

    return JsonResponse(
        {
            "asset_id": str(asset.id),
            "url": url,
            "filename": asset.filename,
        }
    )


_RANGE_HEADER_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


# Response chunk size for streamed media. Matches _RangeFileIterator's default
# so the local and object-storage paths behave alike.
_STREAM_CHUNK_SIZE = 64 * 1024


def _iter_and_close(body, chunk_size=_STREAM_CHUNK_SIZE):
    """Yield an object-storage body's chunks, closing it however iteration ends.

    botocore's ``StreamingBody.iter_chunks`` is a bare ``while True: yield``
    with no ``try/finally``, and Django only registers *this* generator's
    ``close`` on the response — so closing the response unwinds the generator
    without ever calling ``StreamingBody.close()``, and the underlying urllib3
    connection is never returned to the pool. A seeking ``<video>`` aborts range
    requests constantly, which is exactly how a pool of 10 runs dry.
    """
    try:
        yield from body.iter_chunks(chunk_size)
    finally:
        with contextlib.suppress(Exception):
            body.close()


# Frame-picker filmstrips are derived from immutable bytes, so this only bounds
# how long a cache entry squats on memory, not how stale it can get.
FILMSTRIP_CACHE_SECONDS = 60 * 60


class _RangeFileIterator:
    """Iterate a bounded byte window of an already-positioned file handle."""

    def __init__(self, file_handle, remaining, chunk_size=64 * 1024):
        self.file_handle = file_handle
        self.remaining = remaining
        self.chunk_size = chunk_size

    def __iter__(self):
        return self

    def __next__(self):
        if self.remaining <= 0:
            raise StopIteration
        data = self.file_handle.read(min(self.chunk_size, self.remaining))
        if not data:
            raise StopIteration
        self.remaining -= len(data)
        return data

    def close(self):
        if hasattr(self.file_handle, "close"):
            self.file_handle.close()


@login_required
@require_GET
def media_stream(request, workspace_id, asset_id):
    """Stream a media asset through the app's own origin, with Range support.

    The composer's frame picker draws video frames onto a canvas, which the
    browser only allows for same-origin (or CORS-approved) media. Object
    storage like S3/R2 serves signed URLs from another origin, usually
    without CORS headers, so the raw file URL would taint the canvas.

    Byte-range requests matter here: without them the browser can't seek a
    <video> beyond what it has buffered (the scrubber and filmstrip clicks
    silently do nothing) and has to download the whole file up front.
    """
    workspace = _get_workspace(request, workspace_id)

    from apps.media_library.models import MediaAsset

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(
            workspace_id=workspace.id,
            organization_id=workspace.organization_id,
        ),
        pk=asset_id,
    )
    if not asset.file:
        raise Http404
    from apps.media_library.storage import is_s3_backend, open_object_range

    # On object storage, ask for exactly the window the browser asked for.
    # Opening the FieldFile instead would pull the ENTIRE video down before
    # serving a 64 KiB slice — and a seeking <video> issues many such requests,
    # which is how one frame-picker session could exhaust a web dyno.
    remote = is_s3_backend()

    # The DB row can outlive the stored object (lifecycle rule, manual S3
    # deletion); opening/stat-ing it then raises a backend error rather than
    # returning an empty FieldFile, so map that to 404 instead of a 500.
    try:
        size = asset.file.size
        file_handle = None if remote else asset.file.open("rb")
    except Exception:  # noqa: BLE001 - storage backends raise varied errors (OSError, botocore ClientError)
        raise Http404 from None

    content_type = asset.mime_type or "application/octet-stream"
    range_match = _RANGE_HEADER_RE.match(request.headers.get("Range", ""))

    try:
        if range_match and size:
            start_str, end_str = range_match.groups()
            if not start_str:
                # Suffix range: the last N bytes.
                length = min(int(end_str or 0), size)
                start = size - length
                end = size - 1
            else:
                start = int(start_str)
                end = min(int(end_str), size - 1) if end_str else size - 1
            if start >= size or start > end:
                if file_handle is not None:
                    file_handle.close()
                response = HttpResponse(status=416)
                response["Content-Range"] = f"bytes */{size}"
                return response
            if remote:
                body = open_object_range(asset.file.name, start, end)
                response = StreamingHttpResponse(
                    _iter_and_close(body),
                    status=206,
                    content_type=content_type,
                )
            else:
                file_handle.seek(start)
                response = StreamingHttpResponse(
                    _RangeFileIterator(file_handle, end - start + 1),
                    status=206,
                    content_type=content_type,
                )
            response["Content-Length"] = str(end - start + 1)
            response["Content-Range"] = f"bytes {start}-{end}/{size}"
        elif remote:
            body = open_object_range(asset.file.name)
            response = StreamingHttpResponse(
                _iter_and_close(body),
                content_type=content_type,
            )
            response["Content-Length"] = str(size)
        else:
            response = FileResponse(file_handle, content_type=content_type)
    except FileNotFoundError:
        # Only a vanished object is a 404. Any other exception here is a bug in
        # the range arithmetic or the response construction, and swallowing it
        # as a 404 would hide it from the logs while the frame picker silently
        # did nothing.
        if file_handle is not None:
            file_handle.close()
        raise Http404 from None
    except Exception:
        if file_handle is not None:
            file_handle.close()
        logger.exception("media_stream failed for asset %s", asset.pk)
        raise

    response["Accept-Ranges"] = "bytes"
    # Asset files are immutable per id - let the browser cache the stream so
    # reopening the frame picker doesn't re-download the whole video.
    response["Cache-Control"] = "private, max-age=3600"
    return response


@login_required
@require_GET
def media_filmstrip(request, workspace_id, asset_id):
    """Return evenly-spaced thumbnail frames for the frame-picker filmstrip.

    Extracted server-side with ffmpeg, which seeks each frame via byte ranges
    instead of making the browser download (and decode) most of the video to
    build the strip client-side.
    """
    workspace = _get_workspace(request, workspace_id)

    import os
    import tempfile

    from apps.media_library.models import MediaAsset
    from apps.media_library.services import extract_video_frames, extract_video_metadata
    from apps.media_library.storage import download_to_path

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(
            workspace_id=workspace.id,
            organization_id=workspace.organization_id,
        ),
        pk=asset_id,
    )
    if asset.media_type != MediaAsset.MediaType.VIDEO or not asset.file:
        raise Http404

    # An asset's bytes never change once stored, so the strip for a given id is
    # fixed. Worth caching: building it costs a full download plus eight ffmpeg
    # runs inside a single web request, and the picker gets reopened a lot.
    # Its own bounded alias, not the shared default cache: these entries are
    # base64 JPEGs, big enough that 300 of them would be a memory problem of
    # exactly the kind this endpoint was changed to avoid.
    filmstrip_cache = caches["filmstrip"]
    cache_key = f"composer:filmstrip:{asset.pk}"
    cached = filmstrip_cache.get(cache_key)
    if cached is not None:
        return JsonResponse(cached)

    # Mirror media_library's video pipeline: pull the file to one local temp
    # file, then run all the ffmpeg seeks against it. Extracting each frame
    # straight from the (remote) signed URL re-opens the connection and
    # re-reads the moov atom every time - on R2 that was ~1.5s per frame.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{asset.file_extension}", delete=False) as tmp:
            tmp_path = tmp.name
        download_to_path(asset.file, tmp_path)
    except Exception:  # noqa: BLE001 - storage backends raise varied errors when the object is gone
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise Http404 from None

    try:
        duration = asset.duration or extract_video_metadata(tmp_path).get("duration_seconds") or 0
        if not duration:
            return JsonResponse({"error": _("Could not read video duration.")}, status=502)

        count = 8
        timestamps = [round(duration * (i + 0.5) / count, 3) for i in range(count)]
        jpegs = extract_video_frames(tmp_path, timestamps, width=160)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    frames = [
        {"time": t, "dataUrl": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}
        for t, jpg in zip(timestamps, jpegs, strict=False)
        if jpg
    ]
    if not frames:
        return JsonResponse({"error": _("Could not extract frames.")}, status=502)
    payload = {"frames": frames, "duration": duration}
    filmstrip_cache.set(cache_key, payload, FILMSTRIP_CACHE_SECONDS)
    return JsonResponse(payload)


UNSPLASH_API_BASE = "https://api.unsplash.com"
UNSPLASH_IMAGE_HOST = "https://images.unsplash.com/"
UNSPLASH_NOT_CONFIGURED = (
    "Unsplash is not configured. Add UNSPLASH_ACCESS_KEY to your environment to enable stock-photo search."
)
UNSPLASH_MAX_IMPORT = 10
UNSPLASH_MAX_IMAGE_BYTES = 15 * 1024 * 1024


def _as_str(value):
    """Coerce an external/client JSON field to a string.

    Client- and Unsplash-supplied payloads can carry ``null`` (or numbers)
    where we expect a URL/text, so guard before ``.startswith()`` or feeding
    a model's non-null string column - otherwise an ``AttributeError`` (or
    IntegrityError) turns a should-fail-gracefully path into a 500.
    """
    return value if isinstance(value, str) else ""


@login_required
@require_GET
def unsplash_search(request, workspace_id):
    """Proxy an Unsplash photo search so the API key stays server-side.

    Returns results trimmed to what the composer modal needs. Grid images are
    hotlinked from the Unsplash CDN per their API guidelines.
    """
    _get_workspace(request, workspace_id)

    if not settings.UNSPLASH_ACCESS_KEY:
        return JsonResponse({"error": UNSPLASH_NOT_CONFIGURED}, status=503)

    query = request.GET.get("q", "").strip()
    if not query:
        return JsonResponse({"error": _("Missing search query")}, status=400)

    try:
        resp = httpx.get(
            f"{UNSPLASH_API_BASE}/search/photos",
            params={"query": query, "page": 1, "per_page": 24},
            headers={
                "Authorization": f"Client-ID {settings.UNSPLASH_ACCESS_KEY}",
                "Accept-Version": "v1",
            },
            timeout=10.0,
        )
    except httpx.RequestError:
        return JsonResponse({"error": _("Could not reach Unsplash. Try again.")}, status=502)

    if resp.status_code in (401, 403):
        return JsonResponse({"error": "Unsplash rejected the API key. Check UNSPLASH_ACCESS_KEY."}, status=502)
    if resp.status_code == 429:
        return JsonResponse({"error": _("Unsplash rate limit reached. Try again in a few minutes.")}, status=429)
    if resp.status_code != 200:
        return JsonResponse({"error": _("Unsplash search failed. Try again.")}, status=502)

    # A 200 with a malformed body or an unexpected result shape (e.g. a bare
    # list or null instead of an object) must surface as a friendly 502, not
    # an unhandled 500.
    try:
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected Unsplash response shape")
        results = [
            {
                "id": p["id"],
                "thumb": p["urls"]["small"],
                "full": p["urls"]["regular"],
                "width": p.get("width"),
                "height": p.get("height"),
                "color": p.get("color"),
                "alt": p.get("alt_description") or p.get("description") or "",
                "photographer": p["user"]["name"],
                "photographer_url": p["user"]["links"]["html"],
                "photo_url": p["links"]["html"],
                "download_location": p["links"]["download_location"],
            }
            for p in data.get("results", [])
        ]
        total = data.get("total", 0)
    except (ValueError, KeyError, TypeError, AttributeError):
        return JsonResponse({"error": _("Unexpected response from Unsplash. Try again.")}, status=502)
    return JsonResponse({"results": results, "total": total})


@login_required
@require_permission("create_posts")
@require_permission("upload_media")
@require_POST
def unsplash_import(request, workspace_id, post_id=None):
    """Download selected Unsplash photos server-side and attach them as media.

    Mirrors upload_media: attaches to the post when post_id is given, else
    queues in the pending-media session. Hits each photo's download_location
    first, as Unsplash's guidelines require when a photo is actually used.
    """
    workspace = _get_workspace(request, workspace_id)

    if not settings.UNSPLASH_ACCESS_KEY:
        return JsonResponse({"error": UNSPLASH_NOT_CONFIGURED}, status=503)

    try:
        payload = json.loads(request.body)
        photos = payload["photos"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return JsonResponse({"error": "Invalid request body"}, status=400)

    if not isinstance(photos, list) or not photos:
        return JsonResponse({"error": _("No photos selected")}, status=400)
    if len(photos) > UNSPLASH_MAX_IMPORT:
        return JsonResponse(
            {"error": _("Select at most %(count)s photos per import.") % {"count": UNSPLASH_MAX_IMPORT}}, status=400
        )

    from django.core.files.base import ContentFile

    from apps.media_library.models import MediaAsset
    from apps.media_library.quotas import enforce_upload_quotas

    post = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        _require_post_editor(request, post)

    auth_headers = {"Authorization": f"Client-ID {settings.UNSPLASH_ACCESS_KEY}"}
    new_assets = []
    attachments = []
    failed = 0
    quota_error = None

    # follow_redirects is OFF: the host allowlist below is only meaningful if a
    # 30x can't bounce the request to an internal address (SSRF). One pooled
    # client reuses connections across the per-photo registration + download.
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        for photo in photos:
            if not isinstance(photo, dict):
                failed += 1
                continue
            photo_id = _as_str(photo.get("id")).strip()
            download_location = _as_str(photo.get("download_location"))
            fallback_url = _as_str(photo.get("full"))
            # The client supplies these URLs - only ever fetch from Unsplash hosts.
            if not photo_id or not download_location.startswith(f"{UNSPLASH_API_BASE}/"):
                failed += 1
                continue

            content = None
            content_type = ""
            try:
                # Register the download (required by Unsplash API guidelines);
                # the response carries the actual file URL to fetch. A non-JSON
                # body here must fail this one photo, not the whole request.
                dl_resp = client.get(download_location, headers=auth_headers, timeout=10.0)
                image_url = ""
                if dl_resp.status_code == 200:
                    # A non-JSON or non-object body must fail this one photo,
                    # not 500 the whole request.
                    with contextlib.suppress(ValueError, AttributeError):
                        image_url = _as_str(dl_resp.json().get("url"))
                if not image_url:
                    image_url = fallback_url
                if not image_url.startswith(UNSPLASH_IMAGE_HOST):
                    failed += 1
                    continue

                # Stream and cap the download so a huge (or mis-declared) body
                # can't be buffered whole - reject before exceeding the limit.
                with client.stream("GET", image_url) as img_resp:
                    content_type = img_resp.headers.get("content-type", "")
                    declared = img_resp.headers.get("content-length", "")
                    if (
                        img_resp.status_code != 200
                        or not content_type.startswith("image/")
                        or (declared.isdigit() and int(declared) > UNSPLASH_MAX_IMAGE_BYTES)
                    ):
                        failed += 1
                        continue
                    buf = bytearray()
                    for chunk in img_resp.iter_bytes():
                        buf += chunk
                        if len(buf) > UNSPLASH_MAX_IMAGE_BYTES:
                            break
                    if len(buf) > UNSPLASH_MAX_IMAGE_BYTES:
                        failed += 1
                        continue
                    content = bytes(buf)
            except httpx.HTTPError:
                failed += 1
                continue

            try:
                enforce_upload_quotas(workspace.organization, workspace, len(content))
            except (StorageQuotaExceededError, WorkspaceStorageExceededError) as exc:
                # Stop the whole import rather than skipping this photo:
                # every remaining one is at least as large as the headroom
                # we just failed to find, so continuing only wastes
                # Unsplash download calls.
                quota_error = exc
                break

            filename = f"unsplash-{photo_id}.jpg"
            asset = MediaAsset.objects.create(
                organization=workspace.organization,
                workspace=workspace,
                uploaded_by=request.user,
                file=ContentFile(content, name=filename),
                filename=filename,
                media_type=MediaAsset.MediaType.IMAGE,
                mime_type=content_type,
                file_size=len(content),
                source="unsplash",
                source_url=_as_str(photo.get("photo_url")),
                attribution=f"Photo by {_as_str(photo.get('photographer')) or 'Unknown'} on Unsplash",
                alt_text=_as_str(photo.get("alt")),
            )
            new_assets.append(asset)
            attachment = _attach_asset_for_composer(request, workspace, asset, post)
            if attachment is not None:
                attachments.append(attachment)

    if not new_assets:
        if quota_error is not None:
            return _quota_error_response(quota_error)
        return JsonResponse({"error": _("Could not import the selected photos.")}, status=502)

    if post is not None:
        html = render_to_string(
            "composer/partials/media_list.html",
            {"media_attachments": attachments, "post": post, "workspace": workspace},
            request=request,
        )
    else:
        html = render_to_string(
            "composer/partials/media_list_pending.html",
            {"pending_assets": new_assets, "workspace": workspace},
            request=request,
        )

    return JsonResponse(
        {
            "html": html,
            "assets": [{"id": str(a.id), "url": a.file.url} for a in new_assets],
            "failed": failed,
        }
    )


@login_required
@require_GET
def pinterest_boards(request, workspace_id, account_id):
    """Fetch Pinterest boards for board selection in the composer."""
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace, platform="pinterest")

    from apps.credentials.models import resolve_platform_credentials
    from providers import get_provider

    # .env is dominant; admin-entered org credentials are the fallback.
    credentials = resolve_platform_credentials("pinterest", workspace.organization_id)

    provider = get_provider("pinterest", credentials)

    # Refresh token if expiring
    access_token = account.oauth_access_token
    if account.token_expires_at and account.is_token_expiring_soon:
        try:
            access_token = account.refresh_oauth_token(provider, enqueue_backfill=False)
        except Exception:
            return JsonResponse({"error": "Token refresh failed"}, status=502)

    try:
        boards = provider.get_boards(access_token)
    except Exception:
        return JsonResponse({"error": _("Failed to fetch boards")}, status=502)

    return JsonResponse({"boards": [{"id": b.get("id"), "name": b.get("name")} for b in boards]})


@login_required
@require_GET
def facebook_places(request, workspace_id, account_id):
    """Search location Pages for the composer's place picker.

    Serves the Facebook place and the Instagram location alike: Instagram's
    ``location_id`` is the id of one of these Pages, and an Instagram account
    connected through Facebook Login holds a Page token that can search them.

    Keyword search only — Meta retired the endpoint that searched by
    coordinates in 2020 without a replacement.
    """
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(
        SocialAccount, id=account_id, workspace=workspace, platform__in=("facebook", "instagram")
    )

    query = request.GET.get("q", "").strip()
    if not query:
        return JsonResponse({"places": []})

    from apps.credentials.models import resolve_platform_credentials
    from providers import get_provider

    credentials = resolve_platform_credentials("facebook", workspace.organization_id)
    provider = get_provider("facebook", credentials)

    # A refused search answers 200 with the reason, never an empty list on its
    # own: the picker shows it, and composing carries on without a place.
    try:
        places = provider.search_places(account.oauth_access_token, query)
    except ProviderError as exc:
        logger.warning("Place search refused for account %s: %s", account.id, exc)
        if "Authorization Error" in str(exc):
            message = _(
                "Place search isn't enabled for this app on Meta yet: it needs the "
                "Page Public Metadata Access feature approved in App Review."
            )
        else:
            message = _("Could not search places right now. Try again in a moment.")
        return JsonResponse({"places": [], "error": str(message)})
    return JsonResponse({"places": places})


@login_required
@require_GET
def tiktok_creator_info(request, workspace_id, account_id):
    """Fetch TikTok creator info for the composer's TikTok settings panel.

    TikTok's integration guidelines require querying this fresh before each
    post: the allowed privacy levels depend on the app's audit status and the
    creator's account settings.
    """
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace, platform="tiktok")

    from apps.credentials.models import resolve_platform_credentials
    from providers import get_provider

    credentials = resolve_platform_credentials("tiktok", workspace.organization_id)
    provider = get_provider("tiktok", credentials)

    # This endpoint enriches the composer panel; the real privacy/audit gate is
    # enforced at publish time. When the creator-info lookup can't run (no
    # token, expired token, TikTok API down), degrade to a 200 with empty
    # options and let the panel fall back to its defaults, rather than logging
    # a 5xx in the browser on every composer load.
    def _unavailable(reason):
        return JsonResponse(
            {
                "available": False,
                "error": reason,
                "creator_nickname": "",
                "privacy_level_options": [],
                "comment_disabled": False,
                "duet_disabled": False,
                "stitch_disabled": False,
                "max_video_post_duration_sec": None,
            }
        )

    # Refresh token if expiring
    access_token = account.oauth_access_token
    if account.token_expires_at and account.is_token_expiring_soon:
        try:
            access_token = account.refresh_oauth_token(provider, enqueue_backfill=False)
        except Exception:
            return _unavailable("Token refresh failed")

    try:
        info = provider.query_creator_info(access_token)
    except Exception:
        return _unavailable("Failed to fetch creator info")

    return JsonResponse(
        {
            "available": True,
            "creator_nickname": info.get("creator_nickname", ""),
            "privacy_level_options": info.get("privacy_level_options") or [],
            "comment_disabled": bool(info.get("comment_disabled")),
            "duet_disabled": bool(info.get("duet_disabled")),
            "stitch_disabled": bool(info.get("stitch_disabled")),
            "max_video_post_duration_sec": info.get("max_video_post_duration_sec"),
        }
    )


@login_required
@require_permission("create_posts")
@require_POST
def attach_media(request, workspace_id, post_id):
    """Attach a media asset to a post."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    _require_post_editor(request, post)
    media_asset_id = request.POST.get("media_asset_id")

    if not media_asset_id:
        return JsonResponse({"error": "media_asset_id required"}, status=400)

    from apps.media_library.models import MediaAsset

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id),
        id=media_asset_id,
    )

    max_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"]
    position = (max_pos or 0) + 1

    attachment, _created = PostMedia.objects.get_or_create(
        post=post,
        media_asset=asset,
        defaults={"position": position},
    )

    # Option A: changing media on an approved post sends it back for re-approval.
    _send_back_for_review(request, post)

    response = render(
        request,
        "composer/partials/media_list.html",
        {
            "media_attachments": [attachment],
            "post": post,
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


@login_required
@require_permission("create_posts")
@require_POST
def attach_pending_media(request, workspace_id):
    """Attach a library media asset as pending (before post is saved)."""
    workspace = _get_workspace(request, workspace_id)
    media_asset_id = request.POST.get("media_asset_id")

    if not media_asset_id:
        return JsonResponse({"error": "media_asset_id required"}, status=400)

    from apps.media_library.models import MediaAsset

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id),
        id=media_asset_id,
    )

    session_key = f"pending_media_{workspace.id}"
    pending = request.session.get(session_key, [])
    asset_id_str = str(asset.id)
    if asset_id_str not in pending:
        pending.append(asset_id_str)
        request.session[session_key] = pending

    response = render(
        request,
        "composer/partials/media_list_pending.html",
        {
            "pending_assets": [asset],
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


@login_required
@require_permission("create_posts")
@require_POST
def attach_media_batch(request, workspace_id, post_id=None):
    """Attach every asset the picker selected, in the order they were picked.

    One request for the whole selection rather than one per asset: picking
    eight images used to mean eight round trips, each appending a single
    thumbnail, with the post's media order left to however they happened to
    land. Here the caller's order is the carousel's order.

    ``media_asset_ids`` arrives as a repeated POST field, already ordered by
    when each checkbox was ticked.
    """
    workspace = _get_workspace(request, workspace_id)

    post = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        _require_post_editor(request, post)

    # Anything that isn't a UUID is dropped here rather than handed to the
    # ORM, which raises ValidationError on a malformed id and would turn one
    # bad value into a 500 for the whole selection.
    asset_ids = [aid for aid in request.POST.getlist("media_asset_ids") if _is_valid_uuid(aid)]
    if not asset_ids:
        return JsonResponse({"error": "media_asset_ids required"}, status=400)

    # One query for the whole selection, then re-sorted into the order the
    # user clicked — the DB returns them in whatever order it likes, and that
    # order is what the carousel would otherwise be built from. Unknown IDs
    # (deleted mid-session, or another workspace's) simply fall out.
    visible = MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id)
    by_id = {str(asset.id): asset for asset in visible.filter(id__in=asset_ids)}
    assets = [by_id[aid] for aid in asset_ids if aid in by_id]

    if not assets:
        return JsonResponse({"error": "no matching media"}, status=404)

    attachments = []
    pending_assets = []
    for asset in assets:
        attachment = _attach_asset_for_composer(request, workspace, asset, post=post)
        if attachment is not None:
            attachments.append(attachment)
        else:
            pending_assets.append(asset)

    if post is not None:
        # Option A, as in ``attach_media``: editing the media of an approved
        # post sends it back for re-approval.
        _send_back_for_review(request, post)
        response = render(
            request,
            "composer/partials/media_list.html",
            {
                "media_attachments": attachments,
                "post": post,
                "workspace": workspace,
            },
        )
    else:
        response = render(
            request,
            "composer/partials/media_list_pending.html",
            {
                "pending_assets": pending_assets,
                "workspace": workspace,
            },
        )

    response["HX-Trigger"] = "previewUpdate"
    return response


def _attach_asset_for_composer(request, workspace, asset, post=None):
    """Attach an asset to a post, or queue it in the pending-media session.

    Post path: migrates any session-pending media first (can happen when the
    media picker still uses attach_pending_media after autosave created the
    post and updated the upload URL), then appends the asset at the next
    position. Returns the PostMedia attachment, or None on the pending path.
    """
    from apps.media_library.models import MediaAsset

    session_key = f"pending_media_{workspace.id}"

    if post is not None:
        pending_ids = request.session.get(session_key, [])
        if pending_ids:
            existing_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"] or 0
            for idx, pid in enumerate(pending_ids):
                try:
                    pending_asset = MediaAsset.objects.get(id=pid, workspace=workspace)
                    PostMedia.objects.get_or_create(
                        post=post,
                        media_asset=pending_asset,
                        defaults={"position": existing_pos + idx + 1},
                    )
                except MediaAsset.DoesNotExist:
                    continue
            del request.session[session_key]

        max_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"]
        position = (max_pos or 0) + 1
        return PostMedia.objects.create(post=post, media_asset=asset, position=position)

    # No post yet - store pending media IDs in session so they can be
    # attached when the post is eventually saved.
    pending = request.session.get(session_key, [])
    pending.append(str(asset.id))
    request.session[session_key] = pending
    return None


@login_required
@require_permission("create_posts")
@require_permission("upload_media")
@require_POST
def upload_media(request, workspace_id, post_id=None):
    """Upload a file directly from the composer and optionally attach to a post."""
    workspace = _get_workspace(request, workspace_id)
    # Checked before the file is stored, so a refused upload leaves nothing behind.
    post = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        _require_post_editor(request, post)
    uploaded_file = request.FILES.get("file")

    if not uploaded_file:
        return JsonResponse({"error": _("No file provided")}, status=400)

    from apps.media_library.models import MediaFolder

    # The media picker sends the folder it has open, so the file lands there.
    folder = None
    if folder_id := request.POST.get("folder_id"):
        try:
            folder_pk = uuid.UUID(folder_id)
        except ValueError:
            raise Http404 from None
        folder = get_object_or_404(MediaFolder, pk=folder_pk, workspace=workspace)

    try:
        asset = _create_idea_media_asset(workspace, request.user, uploaded_file, folder=folder)
    except ValidationError as exc:
        return _upload_rejected_response(exc)
    except (StorageQuotaExceededError, WorkspaceStorageExceededError) as exc:
        return _quota_error_response(exc)

    if post is not None:
        attachment = _attach_asset_for_composer(request, workspace, asset, post)
        # Option A: changing media on an approved post sends it back for re-approval.
        _send_back_for_review(request, post)
        response = render(
            request,
            "composer/partials/media_list.html",
            {
                "media_attachments": [attachment],
                "post": post,
                "workspace": workspace,
            },
        )
    else:
        _attach_asset_for_composer(request, workspace, asset)
        response = render(
            request,
            "composer/partials/media_list_pending.html",
            {
                "pending_assets": [asset],
                "workspace": workspace,
            },
        )

    response["X-Uploaded-Asset-Id"] = str(asset.id)
    response["X-Uploaded-Asset-Url"] = asset.file.url
    return response


@login_required
@require_permission("create_posts")
@require_POST
def remove_media(request, workspace_id, post_id, media_id):
    """Remove a media attachment from a post."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    _require_post_editor(request, post)
    return _remove_attachment(request, workspace, post, PostMedia.objects.filter(id=media_id, post=post).first())


def _post_media_list_response(request, workspace, post):
    """The post's thumbnails, re-rendered, with the preview told to refresh."""
    response = render(
        request,
        "composer/partials/media_list.html",
        {
            "media_attachments": post.media_attachments.select_related("media_asset").all(),
            "post": post,
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


def _remove_attachment(request, workspace, post, attachment):
    if attachment is not None:
        # Taking the last video off a post already scheduled to TikTok, or the
        # last image off a pin, would leave it queued to fail. Plain text,
        # because the global htmx error toast shows the body as-is and a 4xx
        # doesn't swap.
        reason = media_change_blocker(
            post,
            media_types_after=post_media_types(post, excluding=attachment),
            ordered_media_types_after=post_ordered_media_types(post, excluding=attachment),
        )
        if reason:
            return HttpResponse(reason, status=400, content_type="text/plain")
        attachment.delete()

    # Option A: changing media on an approved post sends it back for re-approval.
    _send_back_for_review(request, post)
    return _post_media_list_response(request, workspace, post)


def _autosaved_attachment(request, workspace, asset_id):
    """The attachment a "pending" thumbnail now stands for, or ``None``.

    The first autosave creates the post and moves the session's pending media
    onto it, but the thumbnails already on screen keep their pending-media
    URLs. Those requests carry the autosaved post id, so the asset can still be
    found — as an attachment of that post, in this workspace.
    """
    post_id = request.POST.get("_autosave_post_id", "").strip()
    if not _is_valid_uuid(post_id):
        return None
    return (
        PostMedia.objects.select_related("post", "media_asset")
        .filter(post_id=post_id, post__workspace=workspace, media_asset_id=asset_id)
        .first()
    )


@login_required
@require_permission("create_posts")
@require_POST
def remove_pending_media(request, workspace_id, asset_id):
    """Remove a pending media asset (before post is saved)."""
    workspace = _get_workspace(request, workspace_id)

    from apps.media_library.models import MediaAsset
    from apps.media_library.services import delete_asset

    session_key = f"pending_media_{workspace.id}"
    pending = request.session.get(session_key, [])
    asset_id_str = str(asset_id)
    was_pending = asset_id_str in pending
    if not was_pending:
        attachment = _autosaved_attachment(request, workspace, asset_id)
        if attachment is not None:
            _require_post_editor(request, attachment.post)
            return _remove_attachment(request, workspace, attachment.post, attachment)
    else:
        pending.remove(asset_id_str)
        request.session[session_key] = pending

    # Delete the asset and its files from storage (R2) — only one this session
    # was holding as pending, never a library asset named by id alone.
    asset = MediaAsset.objects.filter(id=asset_id, workspace=workspace).first() if was_pending else None
    if asset:
        with contextlib.suppress(Exception):
            delete_asset(asset)

    # Return updated pending list
    pending_assets = MediaAsset.objects.filter(id__in=pending, workspace=workspace)
    response = render(
        request,
        "composer/partials/media_list_pending.html",
        {
            "pending_assets": pending_assets,
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


def _crop_asset(request, workspace, asset):
    """Crop ``asset`` into a new asset, or an error response to return as-is.

    Plain-text 400s (the composer shows the body of a failed htmx request in its
    error toast, and a 4xx doesn't swap the thumbnails); a storage-cap breach in
    the composer's own 413 JSON shape.
    """
    from apps.media_library.cropping import crop_from_request

    return crop_from_request(request, workspace, asset, quota_response=_quota_error_response)


@login_required
@require_permission("create_posts")
@require_POST
def crop_media(request, workspace_id, post_id, media_id):
    """Replace one attachment of a saved post with a cropped copy of its image."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    _require_post_editor(request, post)
    if _is_readonly_write(request, post):
        return HttpResponse(
            _("This post has already published and can't be edited."), status=400, content_type="text/plain"
        )
    attachment = get_object_or_404(PostMedia.objects.select_related("media_asset"), id=media_id, post=post)
    return _crop_attachment(request, workspace, post, attachment)


def _crop_attachment(request, workspace, post, attachment):
    cropped, error = _crop_asset(request, workspace, attachment.media_asset)
    if error is not None:
        return error
    attachment.media_asset = cropped
    attachment.save(update_fields=["media_asset"])

    # Option A: changing media on an approved post sends it back for re-approval.
    _send_back_for_review(request, post)
    return _post_media_list_response(request, workspace, post)


@login_required
@require_permission("create_posts")
@require_POST
def crop_format_media(request, workspace_id, post_id):
    """Crop one attachment for a single format of an account, leaving the others as they are.

    ``target`` is ``"<account>:<format>"`` and ``asset_id`` the attachment's
    image. The copy is listed on that format's rows (``set_format_crop``), so
    a Story can go out in 9:16 while the feed keeps the original. Answers JSON
    describing the copy, for the composer to show it in that format.
    """
    from apps.composer.services import set_format_crop

    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    _require_post_editor(request, post)
    acc_id, _sep, fmt = request.POST.get("target", "").partition(":")
    asset_id = request.POST.get("asset_id", "")
    if not (_is_valid_uuid(acc_id) and _is_valid_uuid(asset_id)):
        raise Http404
    attachment = get_object_or_404(PostMedia.objects.select_related("media_asset"), post=post, media_asset_id=asset_id)
    rows = list(
        post.platform_posts.filter(social_account_id=acc_id, social_account__workspace=workspace, format=fmt).exclude(
            status__in=PlatformPost.PROTECTED_STATUSES
        )
    )
    if not rows:
        raise Http404

    cropped, error = _crop_asset(request, workspace, attachment.media_asset)
    if error is not None:
        return error
    set_format_crop(rows, cropped)
    _send_back_for_review(request, post)
    return JsonResponse(
        {
            "target": f"{acc_id}:{fmt}",
            "source": str(attachment.media_asset_id),
            "asset": {
                "id": str(cropped.id),
                "url": cropped.file.url,
                "width": cropped.width,
                "height": cropped.height,
            },
        }
    )


@login_required
@require_permission("create_posts")
@require_POST
def crop_pending_media(request, workspace_id, asset_id):
    """Replace one not-yet-saved upload with a cropped copy, keeping its place."""
    from apps.media_library.models import MediaAsset

    workspace = _get_workspace(request, workspace_id)
    session_key = f"pending_media_{workspace.id}"
    pending = request.session.get(session_key, [])
    if str(asset_id) not in pending:
        attachment = _autosaved_attachment(request, workspace, asset_id)
        if attachment is None:
            raise Http404
        _require_post_editor(request, attachment.post)
        if _is_readonly_write(request, attachment.post):
            return HttpResponse(
                _("This post has already published and can't be edited."), status=400, content_type="text/plain"
            )
        return _crop_attachment(request, workspace, attachment.post, attachment)
    asset = get_object_or_404(MediaAsset, id=asset_id, workspace=workspace)

    cropped, error = _crop_asset(request, workspace, asset)
    if error is not None:
        return error
    pending[pending.index(str(asset_id))] = str(cropped.id)
    request.session[session_key] = pending

    by_id = {str(a.id): a for a in MediaAsset.objects.filter(id__in=pending, workspace=workspace)}
    response = render(
        request,
        "composer/partials/media_list_pending.html",
        {"pending_assets": [by_id[i] for i in pending if i in by_id], "workspace": workspace},
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


@login_required
@require_permission("create_posts")
@require_http_methods(["GET", "POST"])
def manual_publish(request, workspace_id, post_id, platform_post_id):
    """What the "time to publish" notification opens: media, caption, and "I've published it".

    Only reachable for a channel set to be published by hand
    (``platform_extra["delivery"] == "notify"``); see apps/publisher/manual_delivery.
    """
    workspace = _get_workspace(request, workspace_id)
    platform_post = get_object_or_404(
        PlatformPost.objects.select_related("post", "social_account"),
        id=platform_post_id,
        post_id=post_id,
        post__workspace_id=workspace.id,
    )
    if not is_notify_delivery(platform_post.platform_extra):
        raise Http404

    if request.method == "POST":
        extra = {**(platform_post.platform_extra or {})}
        extra.setdefault("manual_published_at", timezone.now().isoformat())
        platform_post.platform_extra = extra
        platform_post.save(update_fields=["platform_extra"])
        messages.success(request, _("Marked as published. Thank you!"))
        return redirect(request.path)

    attachments = platform_post.post.media_attachments.select_related("media_asset").order_by("position")
    return render(
        request,
        "composer/manual_publish.html",
        {
            "workspace": workspace,
            "platform_post": platform_post,
            "caption": platform_post.effective_caption or "",
            "media_items": [_preview_media_item(att.media_asset) for att in attachments],
            "published_by_hand": bool(platform_post.platform_extra.get("manual_published_at")),
        },
    )


@login_required
@require_GET
def drafts_list(request, workspace_id):
    """List all drafts for this workspace."""
    workspace = _get_workspace(request, workspace_id)
    # A post is a "draft" when at least one of its PlatformPost children is in
    # the draft state and none have moved into a more advanced workflow stage.
    # Easiest correct query: any post whose only child statuses are "draft".
    drafts = (
        Post.objects.for_workspace(workspace.id)
        .filter(platform_posts__status="draft")
        .exclude(
            platform_posts__status__in=[
                "pending_review",
                "pending_client",
                "approved",
                "scheduled",
                "publishing",
                "published",
            ]
        )
        .distinct()
        .select_related("author")
        .prefetch_related("platform_posts__social_account")
        .order_by("-updated_at")
    )

    return render(
        request,
        "composer/drafts_list.html",
        {
            "workspace": workspace,
            "drafts": drafts,
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def post_delete(request, workspace_id, post_id):
    """Delete a post or a single platform post via HTMX.

    ``platform_post`` removes that one row (one format of one account);
    ``account`` removes every format the post has on that account. If no
    PlatformPost remains the parent Post is deleted as well. Without either
    parameter the entire Post (and all its PlatformPosts) is deleted.

    ``remote=1`` in the body also deletes the published rows from their
    network, wherever the network allows it (``PlatformPost.network_delete``),
    before their records go. A row the network refuses to delete keeps its
    record and answers 422 with the reason, so nothing vanishes from here
    while still live there.
    """
    from providers.exceptions import ProviderError

    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    # Deleting a post — its record, or the post on a client's profile — is an edit of it.
    if not can_edit_post(post, request.user, _member_permissions(request)):
        return JsonResponse({"error": _("You don't have permission to delete this post.")}, status=403)

    platform_post_id = request.GET.get("platform_post") or request.POST.get("platform_post")
    account_id = request.GET.get("account") or request.POST.get("account")
    scoped = bool(platform_post_id or account_id)
    rows = post.platform_posts.all()
    if scoped:
        if (platform_post_id and not _is_valid_uuid(platform_post_id)) or (
            account_id and not _is_valid_uuid(account_id)
        ):
            raise Http404
        rows = rows.filter(**({"id": platform_post_id} if platform_post_id else {"social_account_id": account_id}))
        if not rows.exists():
            raise Http404

    if request.POST.get("remote") == "1":
        from apps.publisher.engine import delete_from_network
        from apps.social_accounts.error_messages import friendly_delete_error

        for row in rows.select_related("social_account"):
            if row.network_delete != "yes":
                continue
            try:
                delete_from_network(row)
            except ProviderError as exc:
                logger.warning("Could not delete PlatformPost %s from %s: %s", row.id, row.social_account.platform, exc)
                return JsonResponse(
                    {"error": friendly_delete_error(exc)},
                    status=422,
                    headers={"HX-Trigger": "postChanged"},
                )
            logger.info("User %s deleted PlatformPost %s from %s", request.user.pk, row.id, row.social_account.platform)
            # Gone from the network: the record goes now, so a refusal further
            # down the loop can't leave it pointing at a post that no longer exists.
            row.delete()

    if scoped:
        rows.delete()
        # If no platform posts remain, clean up the parent post too.
        if not post.platform_posts.exists():
            post.delete()
    else:
        post.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "postChanged"},
    )


@login_required
@require_POST
def clone_post_view(request, workspace_id, post_id):
    """Clone a post into a fresh draft (Clone / Repost) and open the copy.

    The composer makes published/publishing posts read-only; this is the escape
    hatch to repost — the duplicate starts as a draft with the same content but
    no schedule, so it can be edited and re-queued without touching the original.
    """
    from django.urls import reverse

    from apps.composer.services import clone_post

    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)

    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    if not perms.get("create_posts", False):
        raise PermissionDenied("You do not have permission to create posts.")

    new_post = clone_post(post, author=request.user)
    target = reverse("composer:compose_edit", kwargs={"workspace_id": workspace.id, "post_id": new_post.id})
    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Redirect": target})
    return redirect(target)


# ---------------------------------------------------------------------------
# Create landing page & Idea CRUD
# ---------------------------------------------------------------------------


def _workspace_event(workspace, event_id):
    """The workspace's calendar event with this id, or None (never another workspace's)."""
    from apps.calendar.models import CustomCalendarEvent

    try:
        return CustomCalendarEvent.objects.for_workspace(workspace.id).filter(id=uuid.UUID(str(event_id))).first()
    except (TypeError, ValueError):
        return None


#: One tone per column the idea board is created with, in order: where ideas
#: arrive, are weighed, are approved and are discarded (`.kanban-tone-*`).
_IDEA_COLUMN_TONES = ("brand", "warning", "success", "error")


def _default_idea_column_names():
    """The names the idea board's columns are created with, in the active language."""
    return [
        _("Brainstorming"),
        pgettext("idea column", "Under review"),
        pgettext("idea column", "Approved"),
        pgettext("idea column", "Discarded"),
    ]


def _idea_tasks():
    """The tasks of the idea in the outer query (imported here: the task
    board depends on the composer, not the other way round)."""
    from django.db.models import OuterRef

    from apps.tasks.models import Task

    return Task.objects.filter(idea_id=OuterRef("pk"))


def _idea_columns(workspace, tag=None):
    """Build Kanban columns from IdeaGroup for a workspace, optionally filtered by tag."""
    groups = IdeaGroup.objects.for_workspace(workspace.id).order_by("position", "created_at")

    # Ensure default groups exist for this workspace
    if not groups.exists():
        # Names are stored as data (users rename them), so they are seeded once
        # in the site's language, not in that of whoever opens the board first.
        with override(settings.LANGUAGE_CODE):
            default_names = _default_idea_column_names()
        created_groups = [
            IdeaGroup.objects.create(workspace=workspace, name=name, position=pos)
            for pos, name in enumerate(default_names)
        ]
        # Seed an introductory idea in the first column
        Idea.objects.create(
            workspace=workspace,
            group=created_groups[0],
            title=_("This is a place to plan \u270d\ufe0f your content"),
            description=_(
                "Save your Ideas before converting them into posts. Brainstorm, plan ahead, "
                "and keep everything organized in one place."
            ),
            status=Idea.Status.UNASSIGNED,
            position=0,
        )
        groups = IdeaGroup.objects.for_workspace(workspace.id).order_by("position", "created_at")

    ideas_qs = (
        Idea.objects.for_workspace(workspace.id)
        .select_related("author", "media_asset", "calendar_event")
        .prefetch_related("media_attachments__media_asset", "channels")
        # Whether the idea already went on to the task board (an art request);
        # with ``post``, what tells an idea already in production.
        .annotate(has_task=models.Exists(_idea_tasks()))
        .order_by("position", "-created_at")
    )
    if tag:
        ideas_qs = ideas_qs.filter(tags__contains=[tag])

    grouped_ideas = {str(grp.id): [] for grp in groups}
    for idea in ideas_qs:
        _prepare_idea_for_kanban(idea)

        group_key = str(idea.group_id) if idea.group_id else ""
        if group_key in grouped_ideas:
            grouped_ideas[group_key].append(idea)

    # What each column is for, shown only under a column that still has the
    # name it is created with, in its place: a brand's own column (or an old
    # "Done" still holding ideas) does not get a description that is not its.
    hints = (
        _("First ideas to explore."),
        _("Weighing whether it works and in which format."),
        _("Ideas ready for production."),
        _("Ideas that will not be used."),
    )
    with override(settings.LANGUAGE_CODE):
        default_names = _default_idea_column_names()
    columns = []
    for index, grp in enumerate(groups):
        is_default = index < len(hints) and grp.name == default_names[index]
        columns.append(
            {
                "id": str(grp.id),
                "key": str(grp.id),
                "label": grp.name,
                "hint": hints[index] if is_default else "",
                # The colour says what the column means, so it follows the same
                # rule as the description; any other column takes the brand's.
                "tone": _IDEA_COLUMN_TONES[index] if is_default else "brand",
                "ideas": grouped_ideas.get(str(grp.id), []),
            }
        )

    # All workspace tags from the Tag model
    all_tags = list(Tag.objects.for_workspace(workspace.id).values_list("name", flat=True))

    return columns, all_tags


def _parse_media_asset_ids(raw_ids):
    """Parse ordered media ids from CSV, preserving order and removing duplicates."""
    ordered = []
    seen = set()
    for token in (raw_ids or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            normalized = str(uuid.UUID(token))
        except (ValueError, TypeError, AttributeError):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _build_idea_media_payload(idea):
    """Build ordered media payload and attachment list for Kanban rendering."""
    attachments = [att for att in idea.media_attachments.all() if att.media_asset_id and att.media_asset]
    media_payload = []
    for att in attachments:
        asset = att.media_asset
        media_payload.append(
            {
                "asset_id": str(asset.id),
                "url": asset.file.url if asset.file else "",
                "filename": asset.filename,
                "media_type": asset.media_type,
                "position": att.position,
            }
        )

    # Legacy fallback for ideas that only have the old single media pointer.
    if not media_payload and idea.media_asset_id and idea.media_asset:
        media_payload.append(
            {
                "asset_id": str(idea.media_asset_id),
                "url": idea.media_asset.file.url if idea.media_asset.file else "",
                "filename": idea.media_asset.filename,
                "media_type": idea.media_asset.media_type,
                "position": 0,
            }
        )
    return media_payload, attachments


def _set_idea_channel_formats(idea, workspace, picked):
    """Point ``idea`` at the brand's accounts in ``picked`` (``{"<account id>": [formats]}``),
    cleaned by ``services.clean_channel_formats``."""
    idea.channel_formats = clean_channel_formats(workspace, picked)
    idea.save(update_fields=["channel_formats"])
    idea.channels.set(list(idea.channel_formats))


def _prepare_idea_for_kanban(idea):
    """Attach computed media/tag fields used by Kanban templates."""
    media_payload, attachments = _build_idea_media_payload(idea)
    idea.media_payload_json = json.dumps(media_payload)
    idea.tags_payload_json = json.dumps(idea.tags or [])
    idea.channel_list = list(idea.channels.all())
    # Each channel with its formats; one ticked before formats existed is on the feed post.
    idea.channels_payload_json = json.dumps(
        {
            str(account.id): (idea.channel_formats or {}).get(str(account.id)) or ["default"]
            for account in idea.channel_list
        }
    )
    idea.media_count = len(media_payload)
    idea.cover_media = attachments[0].media_asset if attachments else idea.media_asset
    return idea


def _render_idea_card_fragment(request, idea):
    """Render a single Kanban idea card fragment."""
    _prepare_idea_for_kanban(idea)
    return render_to_string(
        "composer/partials/idea_card.html",
        {
            "idea": idea,
            "group_id": str(idea.group_id) if idea.group_id else "",
        },
        request=request,
    )


def _render_kanban_column_fragment(request, group):
    """Render a single Kanban column fragment."""
    return render_to_string(
        "composer/partials/kanban_column.html",
        {
            "col": {
                "id": str(group.id),
                "key": str(group.id),
                "label": group.name,
                "ideas": [],
            },
        },
        request=request,
    )


def _wants_json_response(request):
    """Detect whether mutation endpoint should return JSON payload."""
    accept = request.headers.get("Accept", "")
    return "application/json" in accept.lower()


def _normalize_media_asset_ids(raw_ids="", extra_ids=None):
    """Normalize media IDs from mixed inputs while preserving first-seen order."""
    chunks = []
    if isinstance(raw_ids, str):
        chunks.append(raw_ids)
    elif raw_ids:
        chunks.extend(str(item) for item in raw_ids)
    if extra_ids:
        chunks.extend(str(item) for item in extra_ids)
    return _parse_media_asset_ids(",".join(chunks))


def _sync_idea_media_attachments(idea, workspace, ordered_asset_ids):
    """Synchronize IdeaMedia rows to match ordered ids and keep cover pointer aligned."""
    from apps.media_library.models import MediaAsset

    normalized_ids = _normalize_media_asset_ids(ordered_asset_ids)
    if normalized_ids:
        valid_assets = set(
            str(aid)
            for aid in MediaAsset.objects.filter(workspace=workspace, id__in=normalized_ids).values_list(
                "id", flat=True
            )
        )
        ordered_ids = [aid for aid in normalized_ids if aid in valid_assets]
    else:
        ordered_ids = []

    existing_attachments = list(idea.media_attachments.all())
    existing = {str(att.media_asset_id): att for att in existing_attachments}
    ids_to_keep = set(ordered_ids)
    ids_to_delete = [att.id for att in existing_attachments if str(att.media_asset_id) not in ids_to_keep]
    if ids_to_delete:
        IdeaMedia.objects.filter(id__in=ids_to_delete).delete()

    to_create = []
    to_update = []
    now = timezone.now()
    for position, asset_id in enumerate(ordered_ids):
        attachment = existing.get(asset_id)
        if attachment:
            if attachment.position != position:
                attachment.position = position
                attachment.updated_at = now
                to_update.append(attachment)
            continue
        to_create.append(
            IdeaMedia(
                idea=idea,
                media_asset_id=asset_id,
                position=position,
            )
        )

    if to_create:
        IdeaMedia.objects.bulk_create(to_create)
    if to_update:
        IdeaMedia.objects.bulk_update(to_update, ["position", "updated_at"])

    cover_id = ordered_ids[0] if ordered_ids else None
    current_cover_id = str(idea.media_asset_id) if idea.media_asset_id else None
    if current_cover_id != cover_id:
        idea.media_asset_id = cover_id
        idea.save(update_fields=["media_asset", "updated_at"])


def _quota_error_response(exc):
    """Render a storage-cap breach as a 413 JSON body.

    Shared by every composer upload view so the two caps produce one shape.
    The workspace cap carries its own translated remedy sentence ("delete
    files or contract extra storage"); the org cap has no self-service
    remedy, so it gets the generic line. Numbers travel alongside the text
    so the front end can draw the meter without a second request.
    """
    from apps.billing.storage_usage import WorkspaceStorageExceededError

    if isinstance(exc, WorkspaceStorageExceededError):
        detail = str(exc.message)
    else:
        detail = _("Storage quota exceeded. Free up space or upgrade your plan.")
    return JsonResponse(
        {
            "error": detail,
            "used_bytes": exc.used,
            "limit_bytes": exc.limit,
            "attempted_bytes": exc.attempted,
        },
        status=413,
    )


def _create_idea_media_asset(workspace, user, uploaded_file, folder=None):
    """Create a MediaAsset from a file uploaded in the composer or an Idea window.

    Goes through the media library's ``create_asset``, so the type and the
    stored extension come from the file's bytes, never from the name or the
    Content-Type the client sent.

    Raises ``ValidationError`` for a file the library does not accept (callers
    answer 400 via ``_upload_rejected_response``) and
    ``StorageQuotaExceededError`` / ``WorkspaceStorageExceededError`` when the
    upload would breach a cap (413 via ``_quota_error_response``).
    """
    from apps.media_library.services import create_asset
    from apps.media_library.tasks import process_media_asset

    asset = create_asset(workspace.organization, workspace, uploaded_file, user, folder=folder)
    asset.source = "upload"
    asset.save(update_fields=["source"])
    process_media_asset(str(asset.id))
    return asset


def _upload_rejected_response(exc):
    """Render a file the media library refused as a 400 JSON body."""
    return JsonResponse({"error": " ".join(exc.messages)}, status=400)


@login_required
@require_permission("create_posts")
@require_POST
def idea_from_template(request, workspace_id, template_id):
    """Put a template on the idea board, as an idea of the brand's own.

    The idea arrives in the first column drawn like the template: its
    category's icon and colour, its title, its text as the description and
    the category as a tag. From there it is sorted like any other idea.
    """
    workspace = _get_workspace(request, workspace_id)
    template = get_object_or_404(templates_for(workspace).prefetch_related("variations"), id=template_id)
    _idea_columns(workspace)  # creates the board on a brand that never opened it
    icon, tone = idea_icons.category_style(template.category)
    icon = template.icon or icon
    idea = Idea.objects.create(
        workspace=workspace,
        author=request.user,
        title=template.title[:255],
        description=template_text(template, request.POST.get("variation")),
        tags=parse_and_truncate_tag_string(str(template.get_category_display())),
        group=IdeaGroup.objects.for_workspace(workspace.id).order_by("position", "created_at").first(),
        status=Idea.Status.UNASSIGNED,
        icon=icon,
        icon_tone=tone,
    )
    # Meant for the brand's accounts on the networks the template suits, on its formats there.
    _set_idea_channel_formats(
        idea, workspace, template_account_formats(template.network_formats, channel_accounts(workspace))
    )
    record_template_use(workspace, template)
    messages.success(request, _("The template is now one of your ideas."))
    return redirect("composer:create_landing", workspace_id=workspace.id)


#: The pages of the create area and the route of each.
_CREATE_PAGES = {
    "ideas": "composer:create_landing",
    "templates": "composer:create_templates",
    "dates": "composer:create_dates",
}


@login_required
@require_permission("create_posts")
def create_landing(request, workspace_id, tab="ideas"):
    """The create area: the idea board, the templates and the special dates.

    Three pages of one area, each with its own address (``tab`` comes from the
    route). An old link with ``?tab=`` is sent to the page it meant. Each page
    loads only what it shows.
    """
    from apps.calendar.services import (
        date_segments_for,
        special_date_categories,
        special_dates_ahead,
    )
    from apps.radar.market_hashtags import market_hashtags_for

    workspace = _get_workspace(request, workspace_id)
    asked = request.GET.get("tab")
    if asked in _CREATE_PAGES and asked != tab:
        return redirect(_CREATE_PAGES[asked], workspace_id=workspace.id)
    tag = request.GET.get("tag")

    columns, all_tags = _idea_columns(workspace, tag)
    today = timezone.localdate()
    context = {
        "workspace": workspace,
        "tab": tab,
        "columns": columns,
        "all_tags": all_tags,
        "tag_groups": tag_groups_payload(workspace.id),
        "active_tag": tag,
        "feeds": Feed.objects.for_workspace(workspace.id),
        "builtin_templates": [],
        "idea_icons": idea_icons.icon_library(),
        "idea_icon_tones": idea_icons.TONES,
        # The accounts the idea windows offer as its channels.
        "idea_channel_options": list(channel_accounts(workspace)),
    }
    if tab in ("ideas", "dates"):
        context["special_dates_ahead"] = special_dates_ahead(workspace, today)
    if tab == "ideas":
        context.update(
            brand_hashtags=brand_hashtags(workspace.id),
            market_hashtags=market_hashtags_for(workspace),
        )
    if tab == "templates":
        context.update(
            builtin_templates=IdeaTemplate.objects.filter(is_active=True),
            template_shelf=template_shelf(workspace),
            submission_form=TemplateSubmissionForm(),
            template_categories=[{"slug": slug, "label": label} for slug, label in IdeaTemplate.Category.choices],
        )
    if tab == "dates":
        date_segments = date_segments_for(workspace)
        context.update(
            date_segments=date_segments,
            chosen_segment_count=sum(1 for item in date_segments if item["chosen"]),
            special_date_categories=special_date_categories(),
        )
    return render(request, "composer/create_landing.html", context)


@login_required
@require_permission("create_posts")
@require_POST
def idea_upload_media(request, workspace_id):
    """Upload media for Idea modals and return an asset id for reliable save binding."""
    workspace = _get_workspace(request, workspace_id)
    uploaded_file = request.FILES.get("file") or request.FILES.get("media")
    if not uploaded_file:
        return JsonResponse({"error": _("No file provided")}, status=400)

    # Sent from the media library window: into the folder open there (one of the brand's).
    from apps.media_library.models import MediaFolder

    folder = None
    if _is_valid_uuid(request.POST.get("folder_id")):
        folder = MediaFolder.objects.filter(workspace=workspace, id=request.POST["folder_id"]).first()
    try:
        asset = _create_idea_media_asset(workspace, request.user, uploaded_file, folder=folder)
    except ValidationError as exc:
        return _upload_rejected_response(exc)
    except (StorageQuotaExceededError, WorkspaceStorageExceededError) as exc:
        return _quota_error_response(exc)
    return JsonResponse(_idea_asset_payload(asset))


def _idea_asset_payload(asset):
    """What an idea's window needs to show and keep one media file."""
    return {
        "asset_id": str(asset.id),
        "filename": asset.filename,
        "url": asset.file.url if asset.file else "",
        "size": asset.file_size,
        "media_type": asset.media_type,
    }


@login_required
@require_permission("create_posts")
@require_POST
def idea_crop_media(request, workspace_id, asset_id):
    """Crop one image of an idea being written into a new asset, and describe it.

    The idea keeps its media in the page until it is saved, so nothing is
    attached here: the window swaps the copy in where the image was. The
    library keeps the original.
    """
    from apps.media_library.models import MediaAsset

    workspace = _get_workspace(request, workspace_id)
    asset = get_object_or_404(MediaAsset, id=asset_id, workspace=workspace)
    cropped, error = _crop_asset(request, workspace, asset)
    if error is not None:
        return error
    return JsonResponse(_idea_asset_payload(cropped))


@login_required
@require_permission("create_posts")
@require_POST
def idea_create(request, workspace_id):
    """Create a new idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    tags = parse_and_truncate_tag_string(request.POST.get("tags", ""))[:MAX_IDEA_TAGS]

    if not title:
        return HttpResponse("Title is required.", status=400)

    # Assign to the specified group or default to the first group
    group_id = request.POST.get("group")
    if group_id:
        group = IdeaGroup.objects.filter(id=group_id, workspace=workspace).first()
    else:
        group = IdeaGroup.objects.for_workspace(workspace.id).order_by("position").first()

    has_multi_media_payload = "media_asset_ids" in request.POST
    media_asset_ids = _normalize_media_asset_ids(request.POST.get("media_asset_ids", ""))

    # Handle media: pre-uploaded IDs (preferred), direct multipart upload fallback,
    # or legacy single pre-uploaded asset id.
    uploaded_file = request.FILES.get("media") or request.FILES.get("file")
    media_asset_id = request.POST.get("media_asset_id", "").strip()
    if uploaded_file:
        try:
            uploaded_asset = _create_idea_media_asset(workspace, request.user, uploaded_file)
        except ValidationError as exc:
            return _upload_rejected_response(exc)
        except (StorageQuotaExceededError, WorkspaceStorageExceededError) as exc:
            return _quota_error_response(exc)
        media_asset_ids.append(str(uploaded_asset.id))
    elif not media_asset_ids and media_asset_id:
        from apps.media_library.models import MediaAsset

        legacy_asset = MediaAsset.objects.filter(id=media_asset_id, workspace=workspace).first()
        if legacy_asset:
            media_asset_ids.append(str(legacy_asset.id))

    media_asset_ids = _normalize_media_asset_ids(media_asset_ids)

    with transaction.atomic():
        idea = Idea.objects.create(
            workspace=workspace,
            author=request.user,
            title=title,
            description=description,
            tags=tags,
            group=group,
            status=Idea.Status.UNASSIGNED,
            media_asset_id=media_asset_ids[0] if media_asset_ids else None,
            icon=idea_icons.clean_icon(request.POST.get("icon")),
            icon_tone=idea_icons.clean_tone(request.POST.get("icon_tone")),
            target_date=parse_date(request.POST.get("target_date") or "") or None,
            calendar_event=_workspace_event(workspace, request.POST.get("calendar_event")),
        )

        if has_multi_media_payload or media_asset_ids:
            _sync_idea_media_attachments(idea, workspace, media_asset_ids)
        if "channel_formats" in request.POST:
            _set_idea_channel_formats(idea, workspace, parse_channel_formats(request.POST["channel_formats"]))

    # Sync any new tags to the Tag model
    _sync_tags_to_model(workspace, tags)
    # An idea started from a special date files the date's tags it kept under
    # a group named after the date.
    date_tags = parse_and_truncate_tag_string(request.POST.get("tag_group_tags", ""))
    add_to_tag_group(
        workspace,
        request.POST.get("tag_group", "").strip()[:100],
        [name for name in date_tags if name in tags],
    )

    if _wants_json_response(request):
        idea = (
            Idea.objects.for_workspace(workspace.id)
            .select_related("media_asset")
            .prefetch_related("media_attachments__media_asset")
            .get(id=idea.id)
        )
        return JsonResponse(
            {
                "ok": True,
                "idea_id": str(idea.id),
                "group_id": str(idea.group_id) if idea.group_id else "",
                "tags": idea.tags or [],
                "tag_groups": tag_groups_payload(workspace.id),
                "card_html": _render_idea_card_fragment(request, idea),
            }
        )

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_edit(request, workspace_id, idea_id):
    """Edit an existing idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    previous_group_id = str(idea.group_id) if idea.group_id else ""

    idea.title = request.POST.get("title", idea.title).strip()
    idea.description = request.POST.get("description", idea.description).strip()
    idea.tags = parse_and_truncate_tag_string(request.POST.get("tags", ""))[:MAX_IDEA_TAGS]
    if "target_date" in request.POST:
        idea.target_date = parse_date(request.POST["target_date"] or "") or None
    if "icon" in request.POST:
        idea.icon = idea_icons.clean_icon(request.POST["icon"])
    if "icon_tone" in request.POST:
        idea.icon_tone = idea_icons.clean_tone(request.POST["icon_tone"])

    group_id = request.POST.get("group", "").strip()
    if group_id:
        group = IdeaGroup.objects.filter(id=group_id, workspace=workspace).first()
        if group:
            idea.group = group

    # Handle media attachments:
    # - preferred: ordered media_asset_ids list
    # - fallback: legacy single media_asset_id + remove_media
    has_multi_media_payload = "media_asset_ids" in request.POST
    media_asset_ids = _normalize_media_asset_ids(request.POST.get("media_asset_ids", ""))
    uploaded_file = request.FILES.get("media") or request.FILES.get("file")
    uploaded_asset = None
    if uploaded_file:
        try:
            uploaded_asset = _create_idea_media_asset(workspace, request.user, uploaded_file)
        except ValidationError as exc:
            return _upload_rejected_response(exc)
        except (StorageQuotaExceededError, WorkspaceStorageExceededError) as exc:
            return _quota_error_response(exc)
        media_asset_ids.append(str(uploaded_asset.id))
    media_asset_ids = _normalize_media_asset_ids(media_asset_ids)

    with transaction.atomic():
        idea.save(
            update_fields=["title", "description", "tags", "group", "target_date", "icon", "icon_tone", "updated_at"]
        )
        if "channel_formats" in request.POST:
            _set_idea_channel_formats(idea, workspace, parse_channel_formats(request.POST["channel_formats"]))

        if has_multi_media_payload:
            _sync_idea_media_attachments(idea, workspace, media_asset_ids)
        else:
            media_asset_id = request.POST.get("media_asset_id", "").strip()
            remove_media = request.POST.get("remove_media") == "true"

            if uploaded_asset:
                _sync_idea_media_attachments(idea, workspace, [str(uploaded_asset.id)])
            elif media_asset_id:
                from apps.media_library.models import MediaAsset

                asset = MediaAsset.objects.filter(id=media_asset_id, workspace=workspace).first()
                if asset:
                    _sync_idea_media_attachments(idea, workspace, [str(asset.id)])
            elif remove_media:
                _sync_idea_media_attachments(idea, workspace, [])

    # Sync any new tags to the Tag model
    _sync_tags_to_model(workspace, idea.tags)

    if _wants_json_response(request):
        idea = (
            Idea.objects.for_workspace(workspace.id)
            .select_related("media_asset")
            .prefetch_related("media_attachments__media_asset")
            .get(id=idea.id)
        )
        return JsonResponse(
            {
                "ok": True,
                "idea_id": str(idea.id),
                "previous_group_id": previous_group_id,
                "group_id": str(idea.group_id) if idea.group_id else "",
                "tags": idea.tags or [],
                "card_html": _render_idea_card_fragment(request, idea),
            }
        )

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_create_post(request, workspace_id, idea_id):
    """Create a new draft post from an idea and return composer redirect metadata."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(
        Idea.objects.for_workspace(workspace.id)
        .select_related("media_asset")
        .prefetch_related("media_attachments__media_asset"),
        id=idea_id,
    )

    tags = []
    if isinstance(idea.tags, list):
        tags = [tag.strip() for tag in idea.tags if isinstance(tag, str) and tag.strip()]

    ordered_media_asset_ids = []
    seen_media_ids = set()
    for attachment in idea.media_attachments.all():
        if not attachment.media_asset_id:
            continue
        media_id = str(attachment.media_asset_id)
        if media_id in seen_media_ids:
            continue
        seen_media_ids.add(media_id)
        ordered_media_asset_ids.append(media_id)

    # Legacy fallback: old ideas may only have the single media pointer set.
    if not ordered_media_asset_ids and idea.media_asset_id:
        ordered_media_asset_ids.append(str(idea.media_asset_id))

    # The post starts on the idea's channels that are still connected, or on
    # every connected account when the idea names none.
    connected_accounts = list(channel_accounts(workspace))
    idea_channels = {account.id for account in idea.channels.all()}
    if idea_channels:
        connected_accounts = [account for account in connected_accounts if account.id in idea_channels]
    # One PlatformPost per format of each channel (the feed post where the idea names none).
    targets = [
        (account, fmt)
        for account in connected_accounts
        for fmt in ((idea.channel_formats or {}).get(str(account.id)) or [PlatformPost.Format.DEFAULT])
    ]

    with transaction.atomic():
        post = Post.objects.create(
            workspace=workspace,
            author=request.user,
            title=idea.title or "",
            caption=idea.description or "",
            tags=tags,
        )

        if ordered_media_asset_ids:
            PostMedia.objects.bulk_create(
                [
                    PostMedia(
                        post=post,
                        media_asset_id=asset_id,
                        position=index,
                    )
                    for index, asset_id in enumerate(ordered_media_asset_ids)
                ]
            )

        if targets:
            PlatformPost.objects.bulk_create(
                [PlatformPost(post=post, social_account=account, format=fmt) for account, fmt in targets]
            )

        idea.post = post
        idea.save(update_fields=["post", "updated_at"])

    from django.urls import reverse

    compose_url = reverse("composer:compose_edit", kwargs={"workspace_id": workspace.id, "post_id": post.id})
    return JsonResponse(
        {
            "ok": True,
            "post_id": str(post.id),
            "compose_url": compose_url,
        }
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_delete(request, workspace_id, idea_id):
    """Delete an idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    group_id = str(idea.group_id) if idea.group_id else ""
    idea.delete()

    if _wants_json_response(request):
        return JsonResponse(
            {
                "ok": True,
                "idea_id": str(idea_id),
                "group_id": group_id,
            }
        )

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_move(request, workspace_id, idea_id):
    """Move an idea to a new column/position via HTMX (drag-and-drop)."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    new_group_id = request.POST.get("group")
    new_position = request.POST.get("position")

    # Support both group-based and legacy status-based moves
    if new_group_id:
        group = IdeaGroup.objects.filter(id=new_group_id, workspace=workspace).first()
        if group:
            idea.group = group
    else:
        new_status = request.POST.get("status")
        if new_status and new_status in dict(Idea.Status.choices):
            idea.status = new_status
    if new_position is not None:
        with contextlib.suppress(ValueError, TypeError):
            idea.position = int(new_position)
    idea.save()

    # No HX-Trigger here - the frontend handles the move optimistically
    return HttpResponse(status=204)


@login_required
@require_permission("create_posts")
@require_GET
def idea_board(request, workspace_id):
    """Return the Kanban board partial for HTMX refresh."""
    workspace = _get_workspace(request, workspace_id)
    tag = request.GET.get("tag")
    columns, all_tags = _idea_columns(workspace, tag)

    return render(
        request,
        "composer/partials/kanban_board.html",
        {
            "workspace": workspace,
            "columns": columns,
            "all_tags": all_tags,
            "active_tag": tag,
        },
    )


# ---------------------------------------------------------------------------
# Idea Group CRUD (Kanban columns)
# ---------------------------------------------------------------------------


@login_required
@require_permission("create_posts")
@require_POST
def idea_group_create(request, workspace_id):
    """Create a new Kanban column via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    name = request.POST.get("name", "").strip()
    if not name:
        if _wants_json_response(request):
            return JsonResponse({"error": _("Name is required.")}, status=400)
        return HttpResponse("Name is required.", status=400)

    max_pos = IdeaGroup.objects.for_workspace(workspace.id).aggregate(models.Max("position"))["position__max"] or 0
    group = IdeaGroup.objects.create(workspace=workspace, name=name, position=max_pos + 1)

    if _wants_json_response(request):
        return JsonResponse(
            {
                "ok": True,
                "group_id": str(group.id),
                "group_name": group.name,
                "column_html": _render_kanban_column_fragment(request, group),
            }
        )

    return HttpResponse(status=204, headers={"HX-Trigger": "ideaChanged"})


@login_required
@require_permission("create_posts")
@require_POST
def idea_group_delete(request, workspace_id, group_id):
    """Delete an empty Kanban column via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    group = get_object_or_404(IdeaGroup, id=group_id, workspace=workspace)

    if group.ideas.exists():
        if _wants_json_response(request):
            return JsonResponse({"error": _("Column must be empty before deleting.")}, status=400)
        return HttpResponse("Column must be empty before deleting.", status=400)

    deleted_group_id = str(group.id)
    group.delete()
    if _wants_json_response(request):
        return JsonResponse({"ok": True, "group_id": deleted_group_id})
    return HttpResponse(status=204, headers={"HX-Trigger": "ideaChanged"})


@login_required
@require_permission("create_posts")
@require_POST
def idea_group_reorder(request, workspace_id):
    """Reorder Kanban columns. Expects JSON body: {"order": ["uuid1", "uuid2", ...]}."""
    workspace = _get_workspace(request, workspace_id)
    try:
        data = json.loads(request.body)
        order = data.get("order", [])
    except (json.JSONDecodeError, AttributeError):
        return HttpResponse("Invalid JSON.", status=400)

    if not order:
        return HttpResponse(status=204)

    groups = {str(g.id): g for g in IdeaGroup.objects.for_workspace(workspace.id)}
    for position, group_id in enumerate(order):
        group = groups.get(group_id)
        if group and group.position != position:
            group.position = position
            group.save(update_fields=["position"])

    return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Content Categories CRUD
# ---------------------------------------------------------------------------


@login_required
def category_list(request, workspace_id):
    """Settings page for managing content categories."""
    workspace = _get_workspace(request, workspace_id)
    categories = ContentCategory.objects.for_workspace(workspace.id)
    form = ContentCategoryForm()

    return render(
        request,
        "composer/categories.html",
        {
            "workspace": workspace,
            "categories": categories,
            "form": form,
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def category_create(request, workspace_id):
    """Create a new content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    form = ContentCategoryForm(request.POST)

    if not form.is_valid():
        return HttpResponse("Invalid data.", status=400)

    category = form.save(commit=False)
    category.workspace = workspace
    max_pos = ContentCategory.objects.for_workspace(workspace.id).aggregate(models.Max("position"))["position__max"]
    category.position = (max_pos or 0) + 1
    category.save()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def category_edit(request, workspace_id, category_id):
    """Edit a content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    category = get_object_or_404(ContentCategory, id=category_id, workspace=workspace)
    form = ContentCategoryForm(request.POST, instance=category)

    if not form.is_valid():
        return HttpResponse("Invalid data.", status=400)

    form.save()
    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def category_delete(request, workspace_id, category_id):
    """Delete a content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    category = get_object_or_404(ContentCategory, id=category_id, workspace=workspace)
    category.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


# ---------------------------------------------------------------------------
# Post Templates
# ---------------------------------------------------------------------------


@login_required
def template_list(request, workspace_id):
    """Settings page for managing post templates."""
    workspace = _get_workspace(request, workspace_id)
    templates = PostTemplate.objects.for_workspace(workspace.id).select_related("created_by")

    return render(
        request,
        "composer/templates_list.html",
        {
            "workspace": workspace,
            "templates": templates,
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def save_as_template(request, workspace_id, post_id):
    """Save the current post as a reusable template."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)

    name = request.POST.get("template_name", "").strip()
    if not name:
        name = f"Template from {post.caption_snippet or 'post'}"

    description = request.POST.get("template_description", "").strip()

    template_data = {
        "caption": post.caption,
        "first_comment": post.first_comment,
        "category_id": str(post.category_id) if post.category_id else None,
        "tags": post.tags,
        "platform_account_ids": [str(pp.social_account_id) for pp in post.platform_posts.all()],
        "media_asset_ids": [str(pm.media_asset_id) for pm in post.media_attachments.all()],
    }

    PostTemplate.objects.create(
        workspace=workspace,
        name=name,
        description=description,
        template_data=template_data,
        created_by=request.user,
    )

    if request.htmx:
        return HttpResponse(
            status=204,
            headers={"HX-Trigger": "templateSaved"},
        )
    return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)


@login_required
@require_permission("create_posts")
@require_POST
def template_delete(request, workspace_id, template_id):
    """Delete a post template."""
    workspace = _get_workspace(request, workspace_id)
    tpl = get_object_or_404(PostTemplate, id=template_id, workspace=workspace)
    tpl.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "templateChanged"},
    )


@login_required
@require_GET
def template_picker(request, workspace_id):
    """HTMX partial returning list of templates for the picker modal."""
    workspace = _get_workspace(request, workspace_id)
    templates = PostTemplate.objects.for_workspace(workspace.id).select_related("created_by")

    return render(
        request,
        "composer/partials/template_picker.html",
        {
            "workspace": workspace,
            "templates": templates,
        },
    )


@login_required
@require_GET
def use_template(request, workspace_id, template_id):
    """Redirect to composer with template data pre-filled."""
    workspace = _get_workspace(request, workspace_id)
    get_object_or_404(PostTemplate, id=template_id, workspace=workspace)

    from django.urls import reverse

    compose_url = reverse("composer:compose", kwargs={"workspace_id": workspace.id})
    return redirect(f"{compose_url}?template={template_id}")


# ---------------------------------------------------------------------------
# CSV Import
# ---------------------------------------------------------------------------


@login_required
@require_permission("create_posts")
def csv_upload(request, workspace_id):
    """Render CSV upload page or handle file upload and show column mapping."""
    workspace = _get_workspace(request, workspace_id)

    if request.method == "POST" and request.FILES.get("csv_file"):
        import csv
        import io

        csv_file = request.FILES["csv_file"]
        if csv_file.size and csv_file.size > MAX_CSV_UPLOAD_BYTES:
            return render(
                request,
                "composer/csv_import.html",
                {"workspace": workspace, "error": _("CSV file too large (max 5 MB).")},
            )
        decoded = csv_file.read().decode("utf-8-sig")
        reader = csv.reader(io.StringIO(decoded))
        rows = list(reader)

        if not rows:
            return render(
                request,
                "composer/csv_import.html",
                {"workspace": workspace, "error": _("CSV file is empty.")},
            )

        headers = rows[0]
        preview_rows = rows[1:6]  # First 5 data rows

        # Auto-detect column mapping
        field_map = {
            "date": ["date", "publish_date", "scheduled_date"],
            "time": ["time", "publish_time", "scheduled_time"],
            "platforms": ["platform", "platforms", "channel", "channels"],
            "caption": ["caption", "text", "content", "message", "body"],
            "media_url": ["media_url", "media", "image_url", "image", "video_url"],
            "category": ["category", "content_category", "type"],
            "tags": ["tags", "labels", "tag"],
            "first_comment": ["first_comment", "comment"],
        }

        auto_mapping = {}
        for col_idx, header in enumerate(headers):
            header_lower = header.strip().lower().replace(" ", "_")
            for field, aliases in field_map.items():
                if header_lower in aliases:
                    auto_mapping[field] = col_idx
                    break

        # Store CSV in session for the next step
        request.session[f"csv_import_{workspace.id}"] = {
            "headers": headers,
            "rows": rows[1:],  # Exclude header
            "filename": csv_file.name,
        }

        return render(
            request,
            "composer/partials/csv_mapping.html",
            {
                "workspace": workspace,
                "headers": headers,
                "preview_rows": preview_rows,
                "auto_mapping": auto_mapping,
                "field_choices": list(field_map.keys()),
            },
        )

    return render(
        request,
        "composer/csv_import.html",
        {"workspace": workspace},
    )


@login_required
@require_permission("create_posts")
@require_POST
def csv_preview(request, workspace_id):
    """Validate CSV rows with the selected column mapping and show preview."""
    workspace = _get_workspace(request, workspace_id)
    csv_data = request.session.get(f"csv_import_{workspace.id}")

    if not csv_data:
        return HttpResponse("No CSV data found. Please upload again.", status=400)

    # Parse column mapping from POST
    mapping = {}
    for field in ["date", "time", "platforms", "caption", "media_url", "category", "tags", "first_comment"]:
        col_idx = request.POST.get(f"map_{field}", "")
        if col_idx != "":
            import contextlib

            with contextlib.suppress(ValueError, TypeError):
                mapping[field] = int(col_idx)

    rows = csv_data["rows"]
    errors = []
    valid_count = 0

    from apps.social_accounts.models import SocialAccount

    valid_platforms = {p[0].lower() for p in SocialAccount._meta.get_field("platform").choices}
    connected_accounts = set(
        SocialAccount.objects.for_workspace(workspace.id)
        .publishable()
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .values_list("platform", flat=True)
    )

    for row_idx, row in enumerate(rows, start=2):  # Row 2 = first data row
        row_errors = []

        # Validate date
        if "date" in mapping:
            date_val = row[mapping["date"]].strip() if mapping["date"] < len(row) else ""
            if date_val:
                try:
                    from datetime import date as date_cls

                    date_cls.fromisoformat(date_val)
                except ValueError:
                    row_errors.append(f"Invalid date format '{date_val}' (expected YYYY-MM-DD)")
            else:
                row_errors.append("Date is empty")

        # Validate platforms
        if "platforms" in mapping:
            platforms_val = row[mapping["platforms"]].strip() if mapping["platforms"] < len(row) else ""
            if platforms_val:
                for p in platforms_val.split(","):
                    p = p.strip().lower()
                    if p and p not in valid_platforms:
                        row_errors.append(f"Unknown platform '{p}'")
                    elif p and p not in connected_accounts:
                        row_errors.append(f"Platform '{p}' is not connected")

        # Validate caption
        if "caption" in mapping:
            caption_val = row[mapping["caption"]].strip() if mapping["caption"] < len(row) else ""
            if not caption_val:
                row_errors.append("Caption is empty")

        if row_errors:
            errors.append({"row": row_idx, "errors": row_errors})
        else:
            valid_count += 1

    # Store mapping in session
    request.session[f"csv_mapping_{workspace.id}"] = mapping

    return render(
        request,
        "composer/partials/csv_validation.html",
        {
            "workspace": workspace,
            "total_rows": len(rows),
            "valid_count": valid_count,
            "errors": errors[:50],  # Show max 50 errors
            "has_more_errors": len(errors) > 50,
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def csv_confirm_import(request, workspace_id):
    """Kick off the CSV import as a background job."""
    workspace = _get_workspace(request, workspace_id)
    csv_data = request.session.get(f"csv_import_{workspace.id}")
    mapping = request.session.get(f"csv_mapping_{workspace.id}")

    if not csv_data or not mapping:
        return HttpResponse("No CSV data found. Please upload again.", status=400)

    from apps.social_accounts.models import SocialAccount

    rows = csv_data["rows"]
    created_count = 0
    error_count = 0
    # Dated rows for platforms a CSV row can never satisfy: it carries no
    # media and no Pinterest board. Those are kept as drafts at their date
    # rather than scheduled to fail, and the result screen says so.
    drafted_count = 0
    drafted_platforms: set[str] = set()
    # A member who needs an approval to schedule imports drafts: the row's date
    # is kept as the proposed time, and the post goes through review from there.
    drafts_only = needs_approval_to_schedule(workspace, _member_permissions(request))

    for row in rows:
        try:
            caption = row[mapping["caption"]].strip() if "caption" in mapping and mapping["caption"] < len(row) else ""
            if not caption:
                error_count += 1
                continue

            post = Post(
                workspace=workspace,
                author=request.user,
                caption=caption,
            )
            initial_pp_status = "draft"

            # Date + time
            if "date" in mapping and mapping["date"] < len(row):
                date_str = row[mapping["date"]].strip()
                time_str = ""
                if "time" in mapping and mapping["time"] < len(row):
                    time_str = row[mapping["time"]].strip()

                if date_str:
                    import zoneinfo

                    ws_tz = workspace.effective_timezone or "UTC"
                    tz = zoneinfo.ZoneInfo(ws_tz)
                    from datetime import time as time_cls

                    d = datetime.strptime(date_str, "%Y-%m-%d").date()
                    t = datetime.strptime(time_str, "%H:%M").time() if time_str else time_cls(9, 0)
                    aware_dt = datetime.combine(d, t).replace(tzinfo=tz)
                    if drafts_only:
                        post.proposed_publish_at = aware_dt
                    else:
                        post.scheduled_at = aware_dt
                        initial_pp_status = "scheduled"

            # First comment
            if "first_comment" in mapping and mapping["first_comment"] < len(row):
                post.first_comment = row[mapping["first_comment"]].strip()

            # Tags
            if "tags" in mapping and mapping["tags"] < len(row):
                tags_raw = row[mapping["tags"]].strip()
                if tags_raw:
                    post.tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

            # Category
            if "category" in mapping and mapping["category"] < len(row):
                cat_name = row[mapping["category"]].strip()
                if cat_name:
                    cat, _created = ContentCategory.objects.get_or_create(
                        workspace=workspace,
                        name=cat_name,
                        defaults={"color": "#3B82F6"},
                    )
                    post.category = cat

            post.save()

            # Platforms
            if "platforms" in mapping and mapping["platforms"] < len(row):
                platforms_str = row[mapping["platforms"]].strip()
                if platforms_str:
                    for p in platforms_str.split(","):
                        p = p.strip().lower()
                        accounts = SocialAccount.objects.filter(
                            workspace=workspace,
                            platform=p,
                            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
                        )
                        for acc in accounts:
                            status = initial_pp_status
                            if status == "scheduled" and publish_blocker(acc, media_types=set()):
                                status = "draft"
                            _pp, created = PlatformPost.objects.get_or_create(
                                post=post,
                                social_account=acc,
                                format=PlatformPost.Format.DEFAULT,
                                defaults={
                                    "status": status,
                                    "scheduled_at": post.scheduled_at,
                                },
                            )
                            if created and status != initial_pp_status:
                                drafted_count += 1
                                drafted_platforms.add(acc.get_platform_display())

            created_count += 1
        except Exception:
            error_count += 1

    # Clean up session data
    request.session.pop(f"csv_import_{workspace.id}", None)
    request.session.pop(f"csv_mapping_{workspace.id}", None)

    return render(
        request,
        "composer/partials/csv_progress.html",
        {
            "workspace": workspace,
            "created_count": created_count,
            "error_count": error_count,
            "total_rows": len(rows),
            "drafted_count": drafted_count,
            "drafted_platforms": get_text_list(sorted(drafted_platforms), _("and")),
        },
    )


# ---------------------------------------------------------------------------
# Tag CRUD (JSON API endpoints)
# ---------------------------------------------------------------------------


def _sync_tags_to_model(workspace, tag_names):
    """Ensure Tag records exist for all given tag names in a workspace."""
    ensure_tags(workspace, tag_names)


@login_required
@require_GET
def tag_list(request, workspace_id):
    """Return workspace tags as JSON, optionally filtered by search query."""
    workspace = _get_workspace(request, workspace_id)
    q = request.GET.get("q", "").strip().lower()
    tags = Tag.objects.for_workspace(workspace.id)
    if q:
        tags = tags.filter(name__icontains=q)
    tag_data = [{"id": str(t.id), "name": t.name} for t in tags[:50]]
    return JsonResponse(tag_data, safe=False)


@login_required
@require_permission("create_posts")
@require_POST
def tag_create(request, workspace_id):
    """Create a new tag and return it as JSON."""
    workspace = _get_workspace(request, workspace_id)
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"error": _("Tag name is required.")}, status=400)
    kind = request.POST.get("kind", Tag.Kind.INTERNAL)
    if kind not in Tag.Kind.values:
        return JsonResponse({"error": _("Unknown tag kind.")}, status=400)
    tag, created = Tag.objects.get_or_create(workspace=workspace, name=name, defaults={"kind": kind})
    # An existing tag keeps its kind: the dropdown reaches here for tags that
    # are merely being reused, and that must not rewrite how they publish.
    return JsonResponse({"id": str(tag.id), "name": tag.name, "kind": tag.kind, "created": created})


@login_required
@require_permission("create_posts")
@require_POST
def tag_group_save(request, workspace_id):
    """Create a tag group, or rename one and set its tags (``group_id``)."""
    workspace = _get_workspace(request, workspace_id)
    name = request.POST.get("name", "").strip()[:100]
    if not name:
        return JsonResponse({"error": _("Group name is required.")}, status=400)
    group = None
    group_id = request.POST.get("group_id", "")
    if group_id:
        if not _is_valid_uuid(group_id):
            raise Http404
        group = get_object_or_404(TagGroup, id=group_id, workspace=workspace)
    tag_names = parse_and_truncate_tag_string(request.POST.get("tags", ""))
    try:
        group = save_tag_group(workspace, name, tag_names, group)
    except TagGroupNameTakenError:
        return JsonResponse({"error": _("A group with this name already exists.")}, status=400)
    return JsonResponse(tag_group_payload(group))


@login_required
@require_permission("create_posts")
@require_POST
def tag_group_delete(request, workspace_id, group_id):
    """Delete a tag group. Its tags stay, on the brand and where they were applied."""
    workspace = _get_workspace(request, workspace_id)
    get_object_or_404(TagGroup, id=group_id, workspace=workspace).delete()
    return JsonResponse({"ok": True})


@login_required
@require_permission("create_posts")
@require_POST
def template_submit(request, workspace_id):
    """A customer sends a template idea; it waits hidden for the operator's approval."""
    workspace = _get_workspace(request, workspace_id)
    form = TemplateSubmissionForm(request.POST, request.FILES)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        return toast_response(tone="error", title=_("The template was not sent."), body=str(first_error))
    try:
        submit_template(
            workspace,
            request.user,
            form.save(commit=False),
            variations=zip(
                request.POST.getlist("variation_title"), request.POST.getlist("variation_body"), strict=False
            ),
        )
    except TemplateSubmissionLimitError:
        return toast_response(
            tone="error",
            title=_("You already have %(count)s templates waiting for approval.")
            % {"count": MAX_PENDING_TEMPLATE_SUBMISSIONS},
        )
    return toast_response(
        tone="success",
        title=_("Template sent. Thank you!"),
        body=_("It reaches every brand's shelf once our team approves it."),
        events={"templateSubmitted": True},
    )


@login_required
@require_permission("create_posts")
@require_POST
def template_favorite_toggle(request, workspace_id, template_id):
    """Save a template of the shelf for the brand, or drop it if it was saved."""
    workspace = _get_workspace(request, workspace_id)
    template = get_object_or_404(templates_for(workspace), id=template_id)
    return JsonResponse({"favorite": toggle_template_favorite(workspace, template)})


@login_required
@require_permission("create_posts")
@require_GET
def tag_manage_list(request, workspace_id):
    """The brand's saved tags with how many ideas and posts use each, for the tag manager."""
    workspace = _get_workspace(request, workspace_id)
    return JsonResponse({"tags": tag_usage(workspace)})


@login_required
@require_permission("create_posts")
@require_POST
def tag_delete(request, workspace_id):
    """Delete a saved tag of the brand, taking it off the ideas and posts that carry it."""
    workspace = _get_workspace(request, workspace_id)
    name = request.POST.get("name", "").strip()
    if not name or not delete_tag(workspace, name):
        return JsonResponse({"error": _("This tag no longer exists.")}, status=404)
    return JsonResponse({"ok": True, "name": name})


# ── Feeds ──────────────────────────────────────────────────────────────────


FEED_EVENTS_PAGE_SIZE = 15
FEED_EVENTS_CACHE_TTL_SECONDS = 10 * 60
_IMG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


def _feed_events_cache_key(workspace_id):
    return f"composer:feed-events:{workspace_id}"


def _normalize_selected_feed_id(selected_feed_id, feeds):
    valid_feed_ids = {str(feed.id) for feed in feeds}
    if selected_feed_id in valid_feed_ids:
        return selected_feed_id
    return "all"


def _coerce_positive_int(value, default=0):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _xml_local_name(tag):
    """Return local XML tag name without namespace."""
    if not tag:
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1].lower()
    if ":" in tag:
        return tag.split(":", 1)[-1].lower()
    return tag.lower()


def _first_child(element, *names):
    wanted = {name.lower() for name in names}
    for child in element:
        if _xml_local_name(child.tag) in wanted:
            return child
    return None


def _first_child_text(element, *names):
    child = _first_child(element, *names)
    if child is None:
        return ""
    return (child.text or "").strip()


def _extract_atom_link(entry):
    for child in entry:
        if _xml_local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        rel = (child.attrib.get("rel") or "").strip().lower()
        if href and rel in ("", "alternate"):
            return href
    for child in entry:
        if _xml_local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        if href:
            return href
    return ""


def _extract_image_url(entry, summary_raw):
    for node in entry.iter():
        name = _xml_local_name(node.tag)
        if name not in {"thumbnail", "content", "enclosure"}:
            continue
        url = (node.attrib.get("url") or node.attrib.get("href") or node.attrib.get("src") or "").strip()
        media_type = (node.attrib.get("type") or "").lower()
        medium = (node.attrib.get("medium") or "").lower()
        if url and (name == "thumbnail" or medium == "image" or media_type.startswith("image/") or not media_type):
            return url

    if summary_raw:
        match = _IMG_SRC_RE.search(summary_raw)
        if match:
            return match.group(1)
    return ""


def _clean_summary(raw_summary):
    if not raw_summary:
        return ""
    return re.sub(r"\s+", " ", strip_tags(raw_summary)).strip()


def _parse_published_at(raw_value):
    if not raw_value:
        return None
    with contextlib.suppress(ValueError, TypeError, OverflowError):
        parsed = date_parser.parse(raw_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _parse_feed_document(xml_content):
    """Parse RSS/Atom document into metadata and raw entries.

    Uses safe_xml_fromstring to bound size and reject DTD/entity-bearing
    payloads (billion-laughs defence).
    """
    if isinstance(xml_content, str):
        xml_content = xml_content.encode("utf-8", errors="replace")
    root = safe_xml_fromstring(xml_content)
    if root is None:
        return None

    root_name = _xml_local_name(root.tag)
    if root_name == "rss":
        channel = _first_child(root, "channel")
        if channel is None:
            return None
        entries = [child for child in channel if _xml_local_name(child.tag) == "item"]
        return {
            "title": _first_child_text(channel, "title"),
            "website_url": _first_child_text(channel, "link"),
            "entries": entries,
            "entry_kind": "rss",
        }

    if root_name == "feed":
        entries = [child for child in root if _xml_local_name(child.tag) == "entry"]
        return {
            "title": _first_child_text(root, "title"),
            "website_url": _extract_atom_link(root),
            "entries": entries,
            "entry_kind": "atom",
        }

    if root_name == "rdf":
        channel = _first_child(root, "channel")
        entries = [child for child in root if _xml_local_name(child.tag) == "item"]
        return {
            "title": _first_child_text(channel, "title") if channel is not None else "",
            "website_url": _first_child_text(channel, "link") if channel is not None else "",
            "entries": entries,
            "entry_kind": "rss",
        }

    return None


def _build_event_from_entry(feed, parsed_feed, entry):
    kind = parsed_feed["entry_kind"]
    if kind == "atom":
        raw_title = _first_child_text(entry, "title")
        raw_link = _extract_atom_link(entry)
        raw_summary = _first_child_text(entry, "summary") or _first_child_text(entry, "content")
        raw_published = (
            _first_child_text(entry, "published")
            or _first_child_text(entry, "updated")
            or _first_child_text(entry, "issued")
        )
    else:
        raw_title = _first_child_text(entry, "title")
        raw_link = _first_child_text(entry, "link")
        raw_summary = (
            _first_child_text(entry, "description")
            or _first_child_text(entry, "summary")
            or _first_child_text(entry, "content")
        )
        raw_published = (
            _first_child_text(entry, "pubDate")
            or _first_child_text(entry, "published")
            or _first_child_text(entry, "updated")
            or _first_child_text(entry, "date")
        )

    title = raw_title or parsed_feed["title"] or feed.name or "Untitled"
    link = raw_link or parsed_feed["website_url"] or feed.website_url
    summary = _clean_summary(raw_summary)
    image_url = _extract_image_url(entry, raw_summary)
    published_at = _parse_published_at(raw_published)
    event_id = (link or f"{feed.id}:{title}:{raw_published}").strip()
    return {
        "event_id": event_id,
        "feed_id": str(feed.id),
        "feed_name": feed.name,
        "feed_favicon_url": feed.favicon_url,
        "feed_website_url": feed.website_url or parsed_feed["website_url"],
        "title": title,
        "link": link,
        "summary": summary,
        "image_url": image_url,
        "published_at": published_at,
    }


def _safe_fetch_feed(url, headers, *, timeout=8.0, max_redirects=5):
    """Fetch *url* with manual redirect handling, re-validating each hop with
    is_safe_url. Returns (response, final_url) on success or (None, None) on
    any reject path (initial-URL failed SSRF check, intermediate hop failed
    SSRF check, broken redirect, transport error).
    """
    if not is_safe_url(url):
        return None, None
    current_url = url
    try:
        response = httpx.get(current_url, headers=headers, timeout=timeout, follow_redirects=False)
    except httpx.RequestError:
        return None, None
    for _hop in range(max_redirects):
        if response.status_code not in (301, 302, 303, 307, 308):
            return response, current_url
        location = response.headers.get("Location")
        if not location:
            return None, None
        next_url = urljoin(current_url, location)
        if not is_safe_url(next_url):
            return None, None
        try:
            response = httpx.get(next_url, headers=headers, timeout=timeout, follow_redirects=False)
        except httpx.RequestError:
            return None, None
        current_url = next_url
    # Too many redirects.
    return None, None


def _fetch_feed_events_for_workspace(feeds):
    """Fetch and aggregate recent events across all workspace feeds.

    Each feed is re-validated against is_safe_url at fetch time (not just at
    add time), and redirects are followed manually with per-hop SSRF checks.
    This closes the DNS-rebind / redirect-bait window that would otherwise
    let a previously-valid feed URL reach internal hosts on subsequent polls.
    """
    if not feeds:
        return []

    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
        "User-Agent": "Connekt Flow RSS Reader/1.0",
    }
    all_events = []
    for feed in feeds:
        response, _final_url = _safe_fetch_feed(feed.url, headers)
        if response is None or response.status_code >= 400:
            continue
        parsed_feed = _parse_feed_document(response.content)
        if not parsed_feed:
            continue
        for entry in parsed_feed["entries"][:50]:
            all_events.append(_build_event_from_entry(feed, parsed_feed, entry))

    deduped_events = []
    seen = set()
    for event in all_events:
        dedupe_key = (event["feed_id"], event["event_id"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped_events.append(event)

    deduped_events.sort(
        key=lambda event: event["published_at"] or datetime(1970, 1, 1, tzinfo=UTC),
        reverse=True,
    )
    return deduped_events


def _get_cached_workspace_feed_events(workspace, feeds, force_refresh=False):
    """Return cached feed events and last refresh time for workspace feeds."""
    cache_key = _feed_events_cache_key(workspace.id)
    signature = tuple((str(feed.id), feed.url, feed.name, feed.website_url) for feed in feeds)
    cached = cache.get(cache_key)
    if cached and not force_refresh and cached.get("signature") == signature:
        return cached.get("events", []), cached.get("fetched_at")

    events = _fetch_feed_events_for_workspace(feeds)
    fetched_at = timezone.now()
    cache.set(
        cache_key,
        {
            "signature": signature,
            "events": events,
            "fetched_at": fetched_at,
        },
        FEED_EVENTS_CACHE_TTL_SECONDS,
    )
    return events, fetched_at


def _filter_events_for_feed(events, selected_feed_id):
    if selected_feed_id == "all":
        return events
    return [event for event in events if event["feed_id"] == selected_feed_id]


def _build_feed_events_context(workspace, selected_feed_id="all", offset=0):
    feeds = list(Feed.objects.for_workspace(workspace.id))
    selected_feed_id = _normalize_selected_feed_id(selected_feed_id, feeds)
    selected_feed = next((feed for feed in feeds if str(feed.id) == selected_feed_id), None)
    events, last_refreshed_at = _get_cached_workspace_feed_events(workspace, feeds)
    filtered_events = _filter_events_for_feed(events, selected_feed_id)
    page_events = filtered_events[offset : offset + FEED_EVENTS_PAGE_SIZE]
    next_offset = offset + FEED_EVENTS_PAGE_SIZE
    has_more = len(filtered_events) > next_offset
    return {
        "feeds": feeds,
        "selected_feed_id": selected_feed_id,
        "selected_feed": selected_feed,
        "events": page_events,
        "next_offset": next_offset,
        "has_more": has_more,
        "last_refreshed_at": last_refreshed_at,
        "total_event_count": len(filtered_events),
    }


def _render_feeds_tab(
    request,
    workspace,
    *,
    show_add_modal=False,
    add_rss_url="",
    add_error="",
    selected_feed_id="all",
):
    """Render the feeds tab partial with modal state and first event page."""
    context = _build_feed_events_context(workspace, selected_feed_id=selected_feed_id, offset=0)
    context.update(
        {
            "workspace": workspace,
            "show_add_modal": show_add_modal,
            "add_rss_url": add_rss_url,
            "add_error": add_error,
        }
    )
    return render(request, "composer/partials/feeds_tab.html", context)


def _validate_rss_url(rss_url):
    """Validate that a URL points to a reachable RSS/Atom XML feed."""
    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
        "User-Agent": "Connekt Flow RSS Validator/1.0",
    }
    response, _final_url = _safe_fetch_feed(rss_url, headers)
    if response is None:
        return False, "Could not reach this URL. Please check the link and try again.", {}

    if response.status_code >= 400:
        return False, "This URL could not be loaded as a feed.", {}

    parsed_feed = _parse_feed_document(response.content)
    if not parsed_feed:
        return False, "This URL is reachable, but it does not appear to be a valid RSS/Atom feed.", {}

    return (
        True,
        "",
        {
            "title": parsed_feed.get("title", "").strip(),
            "website_url": parsed_feed.get("website_url", "").strip(),
        },
    )


@login_required
@require_permission("create_posts")
@require_GET
def feed_list(request, workspace_id):
    """Return the feeds tab partial (empty state or feed list)."""
    workspace = _get_workspace(request, workspace_id)
    selected_feed_id = request.GET.get("feed_id", "all")
    is_append = request.GET.get("append") == "1"
    offset = _coerce_positive_int(request.GET.get("offset"), default=0)

    if is_append:
        context = _build_feed_events_context(workspace, selected_feed_id=selected_feed_id, offset=offset)
        context.update(
            {
                "workspace": workspace,
                "show_empty": False,
            }
        )
        return render(request, "composer/partials/feed_events_batch.html", context)

    return _render_feeds_tab(request, workspace, selected_feed_id=selected_feed_id)


@login_required
@require_permission("create_posts")
@require_POST
def feed_add(request, workspace_id):
    """Add a feed subscription to the workspace."""
    from django.core.exceptions import ValidationError
    from django.core.validators import URLValidator

    workspace = _get_workspace(request, workspace_id)
    rss_url = request.POST.get("rss_url", "").strip()
    name = request.POST.get("name", "").strip()
    website_url = request.POST.get("website_url", "").strip()
    source = request.POST.get("source", "")
    category = request.POST.get("category", "brightbean-favorites")
    selected_feed_id = request.POST.get("feed_id", "all")
    derived_metadata = {}

    if not rss_url:
        if source == "explore":
            return HttpResponse("Feed URL is required.", status=400)
        return _render_feeds_tab(
            request,
            workspace,
            show_add_modal=True,
            add_rss_url=rss_url,
            add_error="Feed URL is required.",
            selected_feed_id=selected_feed_id,
        )

    validator = URLValidator()
    try:
        validator(rss_url)
    except ValidationError:
        if source == "explore":
            return HttpResponse("Invalid URL.", status=400)
        return _render_feeds_tab(
            request,
            workspace,
            show_add_modal=True,
            add_rss_url=rss_url,
            add_error="Invalid URL.",
            selected_feed_id=selected_feed_id,
        )

    if source != "explore":
        is_valid_rss, validation_error, derived_metadata = _validate_rss_url(rss_url)
        if not is_valid_rss:
            return _render_feeds_tab(
                request,
                workspace,
                show_add_modal=True,
                add_rss_url=rss_url,
                add_error=validation_error,
                selected_feed_id=selected_feed_id,
            )

    if Feed.objects.for_workspace(workspace.id).filter(url=rss_url).exists():
        # Already subscribed - if from explore, just re-render explore view
        if source == "explore":
            return _render_explore(request, workspace, category)
        return _render_feeds_tab(
            request,
            workspace,
            show_add_modal=True,
            add_rss_url=rss_url,
            add_error="Already subscribed to this feed.",
            selected_feed_id=selected_feed_id,
        )

    resolved_name = name or derived_metadata.get("title") or rss_url
    resolved_website_url = website_url or derived_metadata.get("website_url", "")
    Feed.objects.create(
        workspace=workspace,
        name=resolved_name,
        url=rss_url,
        website_url=resolved_website_url,
        added_by=request.user,
    )
    cache.delete(_feed_events_cache_key(workspace.id))

    if source == "explore":
        response = _render_explore(request, workspace, category)
        response["HX-Trigger"] = "feedsUpdated"
        return response

    return _render_feeds_tab(request, workspace, selected_feed_id=selected_feed_id)


@login_required
@require_permission("create_posts")
@require_POST
def feed_delete(request, workspace_id, feed_id):
    """Remove a feed subscription."""
    workspace = _get_workspace(request, workspace_id)
    selected_feed_id = request.POST.get("feed_id", "all")
    feed = get_object_or_404(Feed, id=feed_id, workspace=workspace)
    feed.delete()
    cache.delete(_feed_events_cache_key(workspace.id))

    return _render_feeds_tab(request, workspace, selected_feed_id=selected_feed_id)


@login_required
@require_permission("create_posts")
@require_GET
def feed_explore(request, workspace_id):
    """Return the explore feeds modal content for a given category."""
    workspace = _get_workspace(request, workspace_id)
    category = request.GET.get("category", "brightbean-favorites")
    return _render_explore(request, workspace, category)


def _render_explore(request, workspace, category):
    """Shared helper to render the explore feeds partial."""
    from .curated_feeds import get_feed_categories, get_feeds_for_category

    subscribed_urls = set(Feed.objects.for_workspace(workspace.id).values_list("url", flat=True))
    curated = get_feeds_for_category(category)
    for feed in curated:
        feed["subscribed"] = feed["rss"] in subscribed_urls

    return render(
        request,
        "composer/partials/feeds_explore.html",
        {
            "workspace": workspace,
            "categories": get_feed_categories(),
            "active_category": category,
            "curated_feeds": curated,
        },
    )
