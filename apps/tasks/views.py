"""Task board views. Every mutation answers 204 + ``HX-Trigger`` or re-renders the open card."""

from __future__ import annotations

import contextlib
import functools
import json
import uuid
import zoneinfo
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Max
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response, trigger_response
from apps.composer.emoji import emoji_catalogue
from apps.composer.services import channel_accounts, clean_channel_formats, parse_channel_formats, tag_groups_payload
from apps.members.decorators import require_permission, require_workspace_role
from apps.members.models import WorkspaceMembership

from . import creative_formats, services
from .models import Task, TaskChecklist, TaskChecklistItem, TaskColumn

BOARD_CHANGED = "tasksChanged"


def require_board_access(view_func):
    """Any workspace member except the client, who works through the portal instead."""

    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        membership = request.workspace_membership
        if not membership or membership.workspace_role == WorkspaceMembership.WorkspaceRole.CLIENT:
            raise PermissionDenied("The task board is for the team.")
        return view_func(request, *args, **kwargs)

    return _wrapped


def _can_edit(request) -> bool:
    return bool(request.workspace_membership.effective_permissions.get("create_posts"))


def _task(request, task_id):
    return get_object_or_404(services.task_detail_queryset(request.workspace), id=task_id)


def _reference_json(asset) -> dict:
    """A reference image as the task windows' script keeps it: its id and a picture to show."""
    picture = asset.thumbnail if asset.thumbnail else asset.file
    return {"id": str(asset.id), "url": picture.url if picture else ""}


def _render_card(request, task, *, changed=True, focus_new_item=False, focus_checklist=""):
    """The open task's modal content, re-fetched so prefetches reflect the change."""
    task = _task(request, task.pk)
    items = list(task.checklist.all())
    task.checklist_done = sum(1 for item in items if item.is_done)
    task.checklist_total = len(items)
    task.has_publishable_art = False
    # The window's stage buttons, by the task's column.
    task.stage_actions = services.stage_actions(task)
    task.is_art_request = any(item.is_art for item in items)
    art_keys = {item.format_key for item in items if item.is_art}
    post = task.post
    when = (post.scheduled_at or post.proposed_publish_at) if post else None
    # Formatted here, in the workspace's zone (as the composer shows it): the
    # template's date filter would convert to the request's zone instead.
    local = when.astimezone(_workspace_tz(request.workspace)) if when else None
    task.post_date_input = local.strftime("%Y-%m-%dT%H:%M") if local else ""
    task.post_date_display = local.strftime("%d/%m/%Y %H:%M") if local else ""
    # Where the window's fields start from (taskEditForm in tasks/board.html).
    groups = _art_catalogue(request.workspace)
    channel_options = list(channel_accounts(request.workspace))
    # The window's summary, before "Edit" (tasks/partials/_task_summary.html): the arts asked
    # for by network, and the post's channels with their formats.
    titles = {c.pk: c.title for c in task.checklists.all()}
    art_summary: dict[str, list[str]] = {}
    for item in items:
        if item.is_art:
            art_summary.setdefault(titles.get(item.checklist_id, ""), []).append(item.text)
    task.art_summary = [{"network": network, "formats": labels} for network, labels in art_summary.items()]
    task.channel_summary = [
        {
            "account": account,
            "formats": [
                o["short"] for o in account.format_options() if o["format"] in task.channel_formats[str(account.id)]
            ],
        }
        for account in channel_options
        if str(account.id) in task.channel_formats
    ]
    task.edit_initial_json = json.dumps(
        {
            "column": str(task.column_id),
            "assignees": [str(u.pk) for u in task.assignees.all()],
            "tags": task.tags,
            "channels": task.channel_formats,
            "references": [_reference_json(asset) for asset in task.references.all()],
            "wantsArt": bool(task.briefing) or any(item.is_art for item in items) or bool(task.references.all()),
            "wantsPost": bool(task.post_caption or task.channel_formats or task.post_id),
        }
    )
    for item in items:
        fmt = creative_formats.get(item.format_key) if item.is_art else None
        item.ratio_ok = True
        if item.media_asset and item.width and item.height:
            # Against the item's own size: the format's, or the one set in its settings.
            item.ratio_ok = creative_formats.ratio_matches(
                item.width, item.height, item.media_asset.width, item.media_asset.height
            )
        if fmt and item.media_asset:
            task.has_publishable_art = task.has_publishable_art or fmt.is_publishable
    response = render(
        request,
        "tasks/partials/_task_modal.html",
        {
            "task": task,
            "workspace": request.workspace,
            "columns": services.ensure_columns(request.workspace),
            "members": services.assignable_members(request.workspace),
            "tag_suggestions": services.tag_suggestions(request.workspace),
            "channel_options": channel_options,
            # "Request arts": the networks and formats, those already asked for ticked and fixed.
            "groups": groups,
            "first_tab": groups[0]["network"].key if groups else "",
            "selected": art_keys,
            "locked": art_keys,
            "checklist_groups": services.checklist_groups(task, items),
            "priorities": Task.Priority.choices,
            "can_edit": _can_edit(request),
            "can_upload": bool(request.workspace_membership.effective_permissions.get("upload_media")),
            "today": timezone.localdate(),
            "focus_new_item": focus_new_item,
            # The checklist whose "add an item" line stays open (its id, or "loose" for the untitled one).
            "focus_checklist": focus_checklist,
        },
    )
    if changed:
        response["HX-Trigger"] = BOARD_CHANGED
    return response


def _workspace_tz(workspace):
    return zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")


def _parse_local_datetime(value: str, workspace):
    """A ``datetime-local`` value (workspace time) as an aware datetime; blank or invalid → None."""
    try:
        naive = datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=_workspace_tz(workspace))


_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


def _board_config(request) -> dict:
    """What the board's script needs: URL templates, the card to open, translated labels."""
    ws = request.workspace.id
    open_task = request.GET.get("task", "")
    if open_task and not Task.objects.filter(workspace=request.workspace, id__in=_valid_uuid(open_task)).exists():
        open_task = ""
    return {
        "placeholder": _PLACEHOLDER,
        "detailUrl": reverse("tasks:task_detail", kwargs={"workspace_id": ws, "task_id": _PLACEHOLDER}),
        "moveUrl": reverse("tasks:task_move", kwargs={"workspace_id": ws, "task_id": _PLACEHOLDER}),
        "reorderUrl": reverse("tasks:column_reorder", kwargs={"workspace_id": ws}),
        "openTask": open_task,
        "canEdit": _can_edit(request),
        "labels": {
            "moveFailed": _("The task could not be moved. Try again."),
            "cropFailed": _("The image could not be saved. Try again."),
        },
    }


def window_labels() -> dict:
    """The task windows' translated labels (static/js/task-window.js reads them)."""
    return {
        "remove": _("Remove"),
        "checklistTitle": _("Checklist title"),
        "checklistItem": _("Add an item"),
    }


def window_context(request) -> dict:
    """What the new task window (tasks/partials/_new_item_modal.html) needs: the board's
    columns, the people, the networks and formats of "Request arts" and the accounts of
    "Create a post"."""
    workspace = request.workspace
    groups = _art_catalogue(workspace)
    return {
        "workspace": workspace,
        "columns": services.ensure_columns(workspace),
        "members": services.assignable_members(workspace),
        "priorities": Task.Priority.choices,
        "today": timezone.localdate(),
        "groups": groups,
        "first_tab": groups[0]["network"].key if groups else "",
        "channel_options": list(channel_accounts(workspace)),
        "task_window_labels": window_labels(),
    }


@login_required
@require_board_access
@require_permission("create_posts")
@require_GET
def new_task_window(request, workspace_id):
    """The board's new task window alone, filled from what another page sends (a special
    date or one of its suggestions, a template version: the same parameters as the board's
    ``?new``), for that page to open over itself instead of leaving for the board. It
    posts to tasks:task_create like the board's own (tasks/partials/_new_item_window.html)."""
    window = _board_window(request) if any(request.GET.get(key) for key in ("idea", "template", "date")) else None
    if window is None:
        raise Http404("Nothing to open the task window with.")
    context = window_context(request)
    context.update(
        window_initial_json=_window_initial(window),
        task_window_labels=window_labels(),
        tag_suggestions=services.tag_suggestions(request.workspace),
        emoji_categories=emoji_catalogue(),
    )
    return render(request, "tasks/partials/_new_item_window.html", context)


def _parse_day(value):
    """A ``YYYY-MM-DD`` value as a date; blank, malformed or impossible (2026-13-01) → None."""
    try:
        return parse_date(value or "")
    except ValueError:
        return None


def _json_list(value) -> list:
    """A posted JSON array; anything else (blank, malformed, another type) → []."""
    try:
        parsed = json.loads(value or "[]")
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _valid_uuid(value) -> list[str]:
    try:
        return [str(uuid.UUID(value))]
    except (TypeError, ValueError):
        return []


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------


@login_required
@require_board_access
@require_GET
def board(request, workspace_id):
    workspace = request.workspace
    assignee = request.GET.get("assignee", "")
    assignee_id = request.user.pk if assignee == "me" else assignee
    members = services.assignable_members(workspace)
    if assignee_id and str(assignee_id) not in {str(m.pk) for m in members}:
        assignee_id = None
        assignee = ""
    priority = request.GET.get("priority", "")
    if priority not in Task.Priority.values:
        priority = ""
    if assignee == "me":
        assignee_label = _("My tasks")
    else:
        assignee_label = next((m.display_name for m in members if str(m.pk) == str(assignee_id)), "")
    context = {
        "workspace": workspace,
        "columns": services.board_columns(workspace, assignee_id=assignee_id, priority=priority),
        "members": members,
        "priorities": Task.Priority.choices,
        "active_assignee": assignee,
        "active_priority": priority,
        "active_assignee_label": assignee_label,
        "active_priority_label": Task.Priority(priority).label if priority else "",
        "can_edit": _can_edit(request),
        "today": timezone.localdate(),
        "task_board_config": _board_config(request),
        "column_tones": TaskColumn.Tone.choices,
        "tag_suggestions": services.tag_suggestions(workspace),
        "tag_groups": tag_groups_payload(workspace.id),
        "emoji_categories": emoji_catalogue(),
    }
    if request.htmx and request.htmx.target == "task-board":
        return render(request, "tasks/partials/_board.html", context)
    context["task_window_labels"] = window_labels()
    if context["can_edit"]:
        # The new task window, rendered on the board itself; the board's own columns (with
        # their cards) stay as they are.
        context.update({k: v for k, v in window_context(request).items() if k not in context})
        # Sent from another page (an idea, a template, a date, a calendar day, a Radar offer):
        # the window opens on arrival, filled from it.
        window = _board_window(request)
        context["window_initial_json"] = _window_initial(window) if window else ""
        # The media library window opens on the board's own folder.
        context["tasks_folder_id"] = str(services.media_folder(workspace).id)
    return render(request, "tasks/board.html", context)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def task_create(request, workspace_id):
    """The new task window: the title is required; description, priority, due date, assignees,
    tags and named checklists (``checklists``, JSON: ``[{"title", "items": [text, …]}]``) are optional.

    With ``request_art`` (the window's "Request arts" switch) it is an art request: at least
    one of the ticked ``formats``, each network's becoming a checklist ahead of the task's own,
    with the ``briefing``; ``art_items`` (JSON, ``[{"format", "count", "note"}]``) say how many
    of a format and what each shows. With ``make_post`` ("Create a post") it carries the ``post_caption``
    and the channels it goes out on (``channel_formats``, JSON, as an idea's).

    ``idea`` (one of the brand's ideas) links the task to the idea it came from.

    Success fires ``taskCreated`` (the window closes) besides refreshing the board.
    """
    workspace = request.workspace
    fields, art, error = parse_task_post(request)
    if error:
        return toast_response(tone="error", title=error)
    if _valid_uuid(request.POST.get("idea")):
        from apps.composer.models import Idea

        fields["idea"] = get_object_or_404(Idea, id=request.POST["idea"], workspace=workspace)
    if art is not None:
        services.request_arts(workspace, request.user, **art, **fields)
        done = _("Art request created.")
    else:
        services.create_task(workspace, request.user, **fields)
        done = _("Task created.")
    return toast_response(tone="success", title=done, events={BOARD_CHANGED: True, "taskCreated": True})


def parse_task_post(request):
    """The new task window's fields, as posted (tasks/partials/_new_item_modal.html), for
    ``services.create_task``; with "Request arts" on, also the art request's own (``art``:
    formats, briefing, references) for ``services.request_arts``, else ``art`` is None.

    Returns ``(fields, art, error)``: ``error`` is the message to show when the window
    can't be saved as it is. Also used by the pages that post the window elsewhere
    (``apps.radar.views.offer_task``).
    """
    workspace = request.workspace
    title = request.POST.get("title", "").strip()
    if not title:
        return {}, None, _("Give the task a title.")
    art = None
    if request.POST.get("request_art") == "1":
        format_keys = request.POST.getlist("formats")
        if not any(creative_formats.get(k) for k in format_keys):
            return {}, None, _("Choose at least one format.")
        art = {
            "format_keys": format_keys,
            "briefing": request.POST.get("briefing", "").strip(),
            "references": request.POST.getlist("references"),
            # How many of each format and what each shows (the window's art rows).
            "art_items": creative_formats.clean_art_items(_json_list(request.POST.get("art_items"))),
        }
    column = None
    if _valid_uuid(request.POST.get("column")):
        column = get_object_or_404(TaskColumn, id=request.POST["column"], workspace=workspace)
    priority = request.POST.get("priority", "")
    make_post = request.POST.get("make_post") == "1"
    fields = {
        "title": title,
        "post_caption": request.POST.get("post_caption", "").strip() if make_post else "",
        "channel_formats": (
            clean_channel_formats(workspace, parse_channel_formats(request.POST.get("channel_formats")))
            if make_post
            else {}
        ),
        "column": column,
        "description": request.POST.get("description", "").strip(),
        "priority": priority if priority in Task.Priority.values else Task.Priority.MEDIUM,
        "due_date": _parse_day(request.POST.get("due_date")),
        "assignees": services.resolve_assignees(workspace, request.POST.getlist("assignees")),
        "tags": request.POST.getlist("tags"),
        "checklists": services.clean_checklists(_json_list(request.POST.get("checklists"))),
    }
    return fields, art, ""


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def task_move(request, workspace_id, task_id):
    workspace = request.workspace
    task = get_object_or_404(Task, id=task_id, workspace=workspace)
    column = get_object_or_404(TaskColumn, id=request.POST.get("column"), workspace=workspace)
    position = 0
    with contextlib.suppress(TypeError, ValueError):
        position = int(request.POST.get("position", 0))
    services.move_task(task, column, position, request.user)
    return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Task card
# ---------------------------------------------------------------------------


@login_required
@require_board_access
@require_GET
def task_detail(request, workspace_id, task_id):
    return _render_card(request, _task(request, task_id), changed=False)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def task_update(request, workspace_id, task_id):
    """Save one or more of the task's fields; only the fields posted change.

    With ``save`` (the window's "Save task", which posts them all) it answers a toast and
    closes the window; otherwise it re-renders the window.
    """
    workspace = request.workspace
    task = get_object_or_404(Task, id=task_id, workspace=workspace)
    fields = []
    if "title" in request.POST:
        title = request.POST["title"].strip()
        if title:
            task.title = title[:255]
            fields.append("title")
    if "description" in request.POST:
        task.description = request.POST["description"].strip()
        fields.append("description")
    if "briefing" in request.POST:
        task.briefing = request.POST["briefing"].strip()
        fields.append("briefing")
    # "Create a post" in the window (post_sent): on, its caption and channels; off, neither.
    making_post = "post_sent" not in request.POST or request.POST.get("make_post") == "1"
    if "post_caption" in request.POST or not making_post:
        task.post_caption = request.POST.get("post_caption", "").strip() if making_post else ""
        fields.append("post_caption")
    if "channel_formats" in request.POST or not making_post:
        picked = parse_channel_formats(request.POST.get("channel_formats")) if making_post else {}
        task.channel_formats = clean_channel_formats(workspace, picked)
        fields.append("channel_formats")
    if "priority" in request.POST and request.POST["priority"] in Task.Priority.values:
        task.priority = request.POST["priority"]
        fields.append("priority")
    if "due_date" in request.POST:
        task.due_date = _parse_day(request.POST["due_date"])
        fields.append("due_date")
    if fields:
        task.save(update_fields=[*fields, "updated_at"])
    if request.POST.get("request_art") == "1":
        # "Request arts" in the window: the formats ticked there that the task doesn't have yet,
        # and its reference images as they are now.
        services.add_arts(task, [k for k in request.POST.getlist("formats") if creative_formats.get(k)])
        if "references_sent" in request.POST:
            services.set_references(task, request.POST.getlist("references"))
    if "publish_at" in request.POST:
        services.propose_post_date(task, _parse_local_datetime(request.POST["publish_at"], workspace))
    if "tags_sent" in request.POST:
        services.set_tags(task, request.POST.getlist("tags"))
    if "assignees_sent" in request.POST:
        users = services.resolve_assignees(workspace, request.POST.getlist("assignees"))
        services.set_assignees(task, users, request.user)
    if "column" in request.POST:
        column = get_object_or_404(TaskColumn, id=request.POST["column"], workspace=workspace)
        if column.id != task.column_id:
            top = Task.objects.filter(column=column).aggregate(top=Max("position"))["top"]
            services.move_task(task, column, 0 if top is None else top + 1, request.user)
    if "save" in request.POST:
        # "Save task" in the task's window: everything at once, then the window closes.
        return toast_response(tone="success", title=_("Task saved."), events={BOARD_CHANGED: True, "closeTask": True})
    return _render_card(request, task)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def task_delete(request, workspace_id, task_id):
    task = get_object_or_404(Task, id=task_id, workspace=request.workspace)
    task.delete()
    return toast_response(tone="success", title=_("Task deleted."), events={BOARD_CHANGED: True, "closeTask": True})


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def task_stage(request, workspace_id, task_id):
    """The task window's stage buttons ("Start production", "Send for review", "Request changes", "Approve"):
    the card goes to that stage's column (``stage``: start, review, changes, done) and the window closes."""
    task = get_object_or_404(Task, id=task_id, workspace=request.workspace)
    stage = request.POST.get("stage", "")
    if not services.move_to_stage(task, stage, request.user):
        return toast_response(tone="error", title=_("This task can't move to that stage from where it is."))
    done = {
        "start": _("Production started."),
        "review": _("Task sent for review."),
        "changes": _("Changes requested."),
        "done": _("Task approved."),
    }[stage]
    return toast_response(tone="success", title=done, events={BOARD_CHANGED: True, "closeTask": True})


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------


def _item(request, task_id, item_id):
    return get_object_or_404(
        TaskChecklistItem.objects.select_related("task__column"),
        id=item_id,
        task_id=task_id,
        task__workspace=request.workspace,
    )


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def item_add(request, workspace_id, task_id):
    """A to-do at the end of the task, inside the named checklist ``checklist`` (one of this task's) or outside any."""
    task = get_object_or_404(Task, id=task_id, workspace=request.workspace)
    checklist = None
    if _valid_uuid(request.POST.get("checklist")):
        checklist = get_object_or_404(TaskChecklist, id=request.POST["checklist"], task=task)
    text = request.POST.get("text", "").strip()
    if text:
        services.add_item(task, text, checklist)
    return _render_card(request, task, focus_new_item=True, focus_checklist=str(checklist.pk) if checklist else "loose")


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def checklist_add(request, workspace_id, task_id):
    task = get_object_or_404(Task, id=task_id, workspace=request.workspace)
    title = request.POST.get("title", "").strip()
    if title:
        services.add_checklist(task, title)
    return _render_card(request, task)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def checklist_update(request, workspace_id, task_id, checklist_id):
    """Rename a named checklist; a blank title keeps the old one."""
    checklist = get_object_or_404(TaskChecklist, id=checklist_id, task_id=task_id, task__workspace=request.workspace)
    title = request.POST.get("title", "").strip()
    if title:
        checklist.title = title[:120]
        checklist.save(update_fields=["title"])
    return _render_card(request, checklist.task)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def checklist_delete(request, workspace_id, task_id, checklist_id):
    """Delete a named checklist with its items."""
    checklist = get_object_or_404(TaskChecklist, id=checklist_id, task_id=task_id, task__workspace=request.workspace)
    task = checklist.task
    checklist.delete()
    return _render_card(request, task)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def checklist_comment(request, workspace_id, task_id, checklist_id):
    """A comment on a named checklist of a task in review; the window reloads with it."""
    checklist = get_object_or_404(
        TaskChecklist.objects.select_related("task__column"),
        id=checklist_id,
        task_id=task_id,
        task__workspace=request.workspace,
    )
    try:
        services.add_comment(checklist, request.user, request.POST.get("body", ""))
    except services.CommentNotAllowedError:
        return toast_response(tone="error", title=_("Comments are written while the task is in review."))
    return _render_card(request, checklist.task, changed=False)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def item_art_remove(request, workspace_id, task_id, item_id):
    """Take the delivered art off an item (the file stays in the media library)."""
    item = _item(request, task_id, item_id)
    if item.takes_file and item.media_asset_id:
        services.remove_art(item)
    return _render_card(request, item.task)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def item_art_crop(request, workspace_id, task_id, item_id):
    """Replace an item's delivered art with a crop of it, made in the image editor.

    Body: the crop box (``x``, ``y``, ``width``, ``height``) in the image's pixels. The
    copy lands next to the original (the "Tasks" folder); the original stays in the
    library. Refusals answer a plain-text 400, which the editor shows as is.
    """
    from apps.media_library.cropping import crop_from_request

    item = _item(request, task_id, item_id)
    if not item.takes_file:
        return HttpResponse(_("Only images can be cropped."), status=400, content_type="text/plain")
    cropped, error = crop_from_request(request, request.workspace, item.media_asset)
    if error is not None:
        return error
    services.deliver_art(item, cropped, request.user)
    return HttpResponse(status=204, headers={"HX-Trigger": BOARD_CHANGED})


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def item_toggle(request, workspace_id, task_id, item_id):
    item = _item(request, task_id, item_id)
    services.set_item_done(item, not item.is_done, request.user)
    return _render_card(request, item.task)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def item_delete(request, workspace_id, task_id, item_id):
    item = _item(request, task_id, item_id)
    task = item.task
    item.delete()
    return _render_card(request, task)


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def item_settings(request, workspace_id, task_id, item_id):
    """The item's settings (its gear, while editing): whether it asks for a file, and the
    size that file should have."""
    item = _item(request, task_id, item_id)
    services.configure_item(
        item,
        accepts_file=request.POST.get("accepts_file") == "1",
        width=request.POST.get("width"),
        height=request.POST.get("height"),
    )
    return _render_card(request, item.task, changed=False)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def column_create(request, workspace_id):
    """A new column, right after the column ``after`` names (a column of this board), or at the end."""
    workspace = request.workspace
    name = request.POST.get("name", "").strip()
    if not name:
        return toast_response(tone="error", title=_("Give the column a name."))
    after = None
    if _valid_uuid(request.POST.get("after")):
        after = get_object_or_404(TaskColumn, id=request.POST["after"], workspace=workspace)
    services.create_column(workspace, name, after=after)
    return trigger_response({BOARD_CHANGED: True})


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def column_rename(request, workspace_id, column_id):
    """Change the column's name and/or colour; only the fields posted change."""
    column = get_object_or_404(TaskColumn, id=column_id, workspace=request.workspace)
    fields = []
    name = request.POST.get("name", "").strip()
    if name:
        column.name = name[:100]
        fields.append("name")
    if request.POST.get("tone") in TaskColumn.Tone.values:
        column.tone = request.POST["tone"]
        fields.append("tone")
    if fields:
        column.save(update_fields=fields)
    return trigger_response({BOARD_CHANGED: True})


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def column_delete(request, workspace_id, column_id):
    column = get_object_or_404(TaskColumn, id=column_id, workspace=request.workspace)
    had_tasks = column.tasks.exists()
    target = services.delete_column(column, request.user)
    if target is None:
        return toast_response(tone="error", title=_("The board needs at least one column."))
    if had_tasks:
        return toast_response(
            tone="success",
            title=_("Column deleted. Its tasks went to %(column)s.") % {"column": target.name},
            events={BOARD_CHANGED: True},
        )
    return trigger_response({BOARD_CHANGED: True})


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def column_reorder(request, workspace_id):
    """Body: ``{"order": [column ids]}``. Unknown ids are ignored."""
    try:
        order = [str(i) for i in json.loads(request.body).get("order", [])]
    except (ValueError, AttributeError):
        return HttpResponse(status=400)
    columns = {str(c.pk): c for c in TaskColumn.objects.for_workspace(request.workspace.id)}
    changed = []
    for position, column_id in enumerate(order):
        column = columns.get(column_id)
        if column and column.position != position:
            column.position = position
            changed.append(column)
    TaskColumn.objects.bulk_update(changed, ["position"])
    return trigger_response({BOARD_CHANGED: True})


# ---------------------------------------------------------------------------
# The new task window, opened from other pages
# ---------------------------------------------------------------------------

# Where send_to_board leaves what the board's window opens with, for one visit.
_HANDOFF_KEY = "task_window_handoff"
# What a handoff may fill (tasks.services.material_prefill keys).
_HANDOFF_FIELDS = ("title", "description", "briefing", "post_caption")


def send_to_board(request, values, *, note="", submit_to="", hidden=None):
    """Send the user to the task board with its new task window open and filled with
    ``values`` (title, description, briefing, post_caption), for a source the board can't
    read from the URL (a Radar offer: its data and the text edited in its dialog).

    ``submit_to`` is where the window posts instead of ``tasks:task_create`` and ``hidden``
    the name → value pairs it posts along; ``note`` is its "From …" line. Answers the
    ``HX-Redirect`` to the board, which takes the handoff once (``_board_window``).
    """
    request.session[_HANDOFF_KEY] = {
        "workspace": str(request.workspace.id),
        "values": {key: str(values.get(key, "")) for key in _HANDOFF_FIELDS},
        "note": str(note),
        "submit_to": submit_to,
        "hidden": {str(k): str(v) for k, v in (hidden or {}).items()},
    }
    url = reverse("tasks:board", kwargs={"workspace_id": request.workspace.id}) + "?new=handoff"
    return HttpResponse(status=204, headers={"HX-Redirect": url})


def _board_window(request) -> dict | None:
    """What the board's new task window opens with when the board is visited with ``?new``:
    sent from an idea (``idea``), a template (``template`` with ``variation``), a special date
    (``date``, its key in the special-dates tab, with ``day``; with ``suggestion``, the index of
    one of its content suggestions, the task is titled and briefed by that suggestion and
    keeps the date's arts to produce), a day of the calendar (``day`` alone) or by
    send_to_board (``new=handoff``). None without ``?new`` (or a handoff already taken).
    Returns ``{"prefill", "note", "submit_to", "hidden"}``."""
    workspace = request.workspace
    params = request.GET
    if "new" not in params:
        return None
    note, submit_to, hidden = "", "", {}
    if params["new"] == "handoff":
        data = request.session.pop(_HANDOFF_KEY, None)
        if not data or data.get("workspace") != str(workspace.id):
            return None
        prefill = services.material_prefill(workspace, **data["values"])
        note, submit_to, hidden = data["note"], data["submit_to"], data["hidden"]
    elif _valid_uuid(params.get("idea")):
        from apps.composer.models import Idea

        idea = get_object_or_404(
            Idea.objects.prefetch_related("media_attachments__media_asset"), id=params["idea"], workspace=workspace
        )
        prefill = services.prefill_from_idea(idea)
        note = _("From the idea “%(title)s”") % {"title": idea.title}
        hidden = {"idea": str(idea.pk)}
    elif params.get("template", "").isdigit():
        from apps.composer.services import templates_for

        template = get_object_or_404(templates_for(workspace).prefetch_related("variations"), id=params["template"])
        prefill = services.prefill_from_template(workspace, template, params.get("variation"))
        note = _("From the template “%(title)s”") % {"title": template.title}
    elif params.get("date") and _parse_day(params.get("day")):
        from apps.calendar.holidays import date_by_key

        day = _parse_day(params["day"])
        entry = date_by_key(params["date"], day, workspace)
        if entry is None:
            raise Http404("No such special date for this brand on that day.")
        index = params.get("suggestion", "")
        suggestions = entry["suggestions"]
        if index:
            if not index.isdigit() or int(index) >= len(suggestions):
                raise Http404("No such suggestion for this special date.")
            suggestion = suggestions[int(index)]
            prefill = services.prefill_from_date(
                workspace,
                title=f"{entry['name']} · {suggestion['title']}",
                day=day,
                brief=entry["description"],
                tags=entry["tags"],
                briefing=suggestion["brief"] or entry["briefing"],
                art_items=entry["art_items"],
            )
        else:
            prefill = services.prefill_from_date(
                workspace,
                title=entry["name"],
                day=day,
                brief=entry["description"],
                tags=entry["tags"],
                briefing=entry["briefing"],
                art_items=entry["art_items"],
            )
        note = _("From the special date “%(title)s”") % {"title": entry["name"]}
    else:
        prefill = services.prefill_for_day(workspace, _parse_day(params.get("day")))
    return {"prefill": prefill, "note": note, "submit_to": submit_to, "hidden": hidden}


def _window_initial(window) -> str:
    """The board's new task window's data-initial for ``window`` (_board_window), as
    newItemForm() (static/js/task-window.js) reads it: the fields' text, the state of its
    pickers and switches, the arts to tick with their count and note, where it posts and
    what along."""
    prefill = window["prefill"]
    due = prefill["due_date"]
    return json.dumps(
        {
            "fields": {
                "title": prefill["title"],
                "description": prefill["description"],
                "briefing": prefill["briefing"],
                "post_caption": prefill["post_caption"],
                "due_date": due.isoformat() if due else "",
            },
            "assignees": [str(u.pk) for u in prefill["assignees"]],
            "tags": prefill["tags"],
            "channels": prefill["channels"],
            "references": [_reference_json(asset) for asset in prefill["references"]],
            "wantsArt": prefill["wants_art"],
            "wantsPost": prefill["wants_post"],
            "artItems": prefill["art_items"],
            "note": window["note"],
            "submitTo": window["submit_to"],
            "hidden": window["hidden"],
        }
    )


def _art_catalogue(workspace) -> list[dict]:
    """The networks to ask arts for, by the workspace's connected accounts, with their formats."""
    from apps.social_accounts.models import PlatformVisibility

    accounts = list(channel_accounts(workspace))
    visible = [value for value, _label in PlatformVisibility.visible_choices()]
    return creative_formats.catalogue(accounts, visible)


@login_required
@require_board_access
@require_workspace_role("manager")
@require_POST
def art_settings(request, workspace_id):
    """Who art requests go to by default, and how many days before the content date they are due."""
    workspace = request.workspace
    task_settings = services.art_settings(workspace)
    assignee = services.resolve_assignees(workspace, [request.POST.get("art_assignee", "")])
    task_settings.art_assignee = assignee[0] if assignee else None
    with contextlib.suppress(TypeError, ValueError):
        task_settings.art_lead_days = max(0, min(60, int(request.POST.get("art_lead_days", ""))))
    task_settings.save()
    return toast_response(tone="success", title=_("Art request settings saved."))


# ---------------------------------------------------------------------------
# Art delivery and the post made from it
# ---------------------------------------------------------------------------


@login_required
@require_board_access
@require_permission("upload_media")
@require_POST
def item_deliver(request, workspace_id, task_id, item_id):
    """The file of an art item: uploaded here (``file``, into the media library's "Tasks"
    folder) or picked in the media library (``asset_id``, one of the workspace's images
    or videos, left where it is).

    Warns when its proportions don't match the format.
    """
    from django.core.exceptions import ValidationError

    from apps.billing.storage_usage import WorkspaceStorageExceededError
    from apps.media_library.models import MediaAsset
    from apps.media_library.quotas import StorageQuotaExceededError
    from apps.media_library.services import create_asset, extract_image_metadata
    from apps.media_library.tasks import process_media_asset

    item = _item(request, task_id, item_id)
    uploaded = request.FILES.get("file")
    picked = None
    if not uploaded and _valid_uuid(request.POST.get("asset_id")):
        picked = MediaAsset.objects.filter(
            workspace=request.workspace,
            id=request.POST["asset_id"],
            media_type__in=[MediaAsset.MediaType.IMAGE, MediaAsset.MediaType.GIF, MediaAsset.MediaType.VIDEO],
        ).first()
    if not item.takes_file or not (uploaded or picked):
        return toast_response(tone="error", title=_("Choose the file of the art."))
    workspace = request.workspace
    if picked:
        return _delivered(request, item, picked)
    try:
        asset = create_asset(
            workspace.organization, workspace, uploaded, request.user, folder=services.media_folder(workspace)
        )
    except WorkspaceStorageExceededError as exc:
        return toast_response(tone="error", title=_("The art was not uploaded."), body=str(exc.message))
    except StorageQuotaExceededError:
        return toast_response(
            tone="error",
            title=_("The art was not uploaded."),
            body=_("Storage quota exceeded. Free up space or upgrade your plan."),
        )
    except ValidationError as exc:
        return toast_response(tone="error", title=_("The art was not uploaded."), body=" ".join(exc.messages))

    if asset.media_type in (asset.MediaType.IMAGE, asset.MediaType.GIF):
        # Read now rather than waiting for the worker, so the size check below
        # is about this file; processing later writes the same numbers.
        size = extract_image_metadata(asset.file)
        if size:
            asset.width, asset.height = size["width"], size["height"]
            asset.save(update_fields=["width", "height"])
    process_media_asset(str(asset.id))
    return _delivered(request, item, asset)


def _delivered(request, item, asset):
    """Deliver ``asset`` on the art item and answer the task's window, warning when its
    proportions don't match the format."""
    matches = services.deliver_art(item, asset, request.user)
    response = _render_card(request, item.task)
    if not matches:
        response["HX-Trigger"] = json.dumps(
            {
                BOARD_CHANGED: True,
                "showToast": {
                    "tone": "warn",
                    "title": _("The art's proportions don't match the format"),
                    "body": _("Expected %(w)s × %(h)s, received %(rw)s × %(rh)s.")
                    % {"w": item.width, "h": item.height, "rw": asset.width, "rh": asset.height},
                },
            }
        )
    return response


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def task_create_post(request, workspace_id, task_id):
    """The post made from the task's delivered arts, once the task is approved (in the done column)."""
    task = get_object_or_404(
        Task.objects.select_related("idea", "workspace", "column"), id=task_id, workspace=request.workspace
    )
    if not task.column.is_done:
        return toast_response(tone="error", title=_("Approve the task before creating its post."))
    try:
        post = services.create_post_from_task(task, request.user)
    except services.NothingToPublishError:
        return toast_response(
            tone="error",
            title=_("There is no delivered art to publish."),
            body=_("Deliver a publishable format for an account connected to this workspace."),
        )
    url = reverse("composer:compose_edit", kwargs={"workspace_id": request.workspace.id, "post_id": post.id})
    return HttpResponse(status=204, headers={"HX-Redirect": url})
