"""Task board views. Every mutation answers 204 + ``HX-Trigger`` or re-renders the open card."""

from __future__ import annotations

import contextlib
import functools
import json
import uuid
import zoneinfo
from datetime import datetime, timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Max
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.common.htmx import toast_response, trigger_response
from apps.members.decorators import require_permission, require_workspace_role
from apps.members.models import WorkspaceMembership

from . import creative_formats, services
from .models import Task, TaskChecklistItem, TaskColumn

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


def _render_card(request, task, *, changed=True, focus_new_item=False):
    """The open task's modal content, re-fetched so prefetches reflect the change."""
    task = _task(request, task.pk)
    items = list(task.checklist.all())
    task.checklist_done = sum(1 for item in items if item.is_done)
    task.has_publishable_art = False
    post = task.post
    when = (post.scheduled_at or post.proposed_publish_at) if post else None
    # Formatted here, in the workspace's zone (as the composer shows it): the
    # template's date filter would convert to the request's zone instead.
    local = when.astimezone(_workspace_tz(request.workspace)) if when else None
    task.post_date_input = local.strftime("%Y-%m-%dT%H:%M") if local else ""
    task.post_date_display = local.strftime("%d/%m/%Y %H:%M") if local else ""
    for item in items:
        fmt = creative_formats.get(item.format_key) if item.is_art else None
        item.ratio_ok = True
        if fmt and item.media_asset:
            item.ratio_ok = creative_formats.ratio_matches(
                fmt.width, fmt.height, item.media_asset.width, item.media_asset.height
            )
            task.has_publishable_art = task.has_publishable_art or fmt.is_publishable
    response = render(
        request,
        "tasks/partials/_task_modal.html",
        {
            "task": task,
            "workspace": request.workspace,
            "columns": services.ensure_columns(request.workspace),
            "members": services.assignable_members(request.workspace),
            "priorities": Task.Priority.choices,
            "can_edit": _can_edit(request),
            "can_upload": bool(request.workspace_membership.effective_permissions.get("upload_media")),
            "today": timezone.localdate(),
            "focus_new_item": focus_new_item,
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
        "labels": {"moveFailed": _("The task could not be moved. Try again.")},
    }


def _parse_day(value):
    """A ``YYYY-MM-DD`` value as a date; blank, malformed or impossible (2026-13-01) → None."""
    try:
        return parse_date(value or "")
    except ValueError:
        return None


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
        "art_settings": services.art_settings(workspace),
        "can_manage": request.workspace_membership.workspace_role
        in (WorkspaceMembership.WorkspaceRole.OWNER, WorkspaceMembership.WorkspaceRole.MANAGER),
    }
    if request.htmx and request.htmx.target == "task-board":
        return render(request, "tasks/partials/_board.html", context)
    return render(request, "tasks/board.html", context)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def task_create(request, workspace_id):
    """The "Add task" window: the title is required; description, priority, due date and assignees are optional.

    Success fires ``taskCreated`` (the window closes) besides refreshing the board.
    """
    workspace = request.workspace
    title = request.POST.get("title", "").strip()
    if not title:
        return toast_response(tone="error", title=_("Give the task a title."))
    column = None
    if _valid_uuid(request.POST.get("column")):
        column = get_object_or_404(TaskColumn, id=request.POST["column"], workspace=workspace)
    priority = request.POST.get("priority", "")
    services.create_task(
        workspace,
        request.user,
        title=title,
        column=column,
        description=request.POST.get("description", "").strip(),
        priority=priority if priority in Task.Priority.values else Task.Priority.MEDIUM,
        due_date=_parse_day(request.POST.get("due_date")),
        assignees=services.resolve_assignees(workspace, request.POST.getlist("assignees")),
    )
    return toast_response(tone="success", title=_("Task created."), events={BOARD_CHANGED: True, "taskCreated": True})


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
    """Save one or more of the card's fields; only the fields posted change."""
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
    if "post_caption" in request.POST:
        task.post_caption = request.POST["post_caption"].strip()
        fields.append("post_caption")
    if "priority" in request.POST and request.POST["priority"] in Task.Priority.values:
        task.priority = request.POST["priority"]
        fields.append("priority")
    if "due_date" in request.POST:
        task.due_date = _parse_day(request.POST["due_date"])
        fields.append("due_date")
    if fields:
        task.save(update_fields=[*fields, "updated_at"])
    if "publish_at" in request.POST:
        services.propose_post_date(task, _parse_local_datetime(request.POST["publish_at"], workspace))
    if "assignees_sent" in request.POST:
        users = services.resolve_assignees(workspace, request.POST.getlist("assignees"))
        services.set_assignees(task, users, request.user)
    if "column" in request.POST:
        column = get_object_or_404(TaskColumn, id=request.POST["column"], workspace=workspace)
        if column.id != task.column_id:
            top = Task.objects.filter(column=column).aggregate(top=Max("position"))["top"]
            services.move_task(task, column, 0 if top is None else top + 1, request.user)
    return _render_card(request, task)


@login_required
@require_board_access
@require_permission("create_posts")
@require_POST
def task_delete(request, workspace_id, task_id):
    task = get_object_or_404(Task, id=task_id, workspace=request.workspace)
    task.delete()
    return toast_response(tone="success", title=_("Task deleted."), events={BOARD_CHANGED: True, "closeTask": True})


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
    task = get_object_or_404(Task, id=task_id, workspace=request.workspace)
    text = request.POST.get("text", "").strip()
    if text:
        top = task.checklist.aggregate(top=Max("position"))["top"]
        TaskChecklistItem.objects.create(task=task, text=text[:255], position=0 if top is None else top + 1)
    return _render_card(request, task, focus_new_item=True)


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
# Art requests
# ---------------------------------------------------------------------------


@login_required
@require_board_access
@require_permission("create_posts")
@require_http_methods(["GET", "POST"])
def request_arts(request, workspace_id):
    """Pick the formats a piece of content needs; creates one task with an item per format."""
    from apps.composer.models import Idea

    workspace = request.workspace
    idea = None
    idea_id = request.GET.get("idea") or request.POST.get("idea")
    if idea_id and _valid_uuid(idea_id):
        idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    task_settings = services.art_settings(workspace)

    if request.method == "POST":
        format_keys = request.POST.getlist("formats")
        title = request.POST.get("title", "").strip()
        error = ""
        if not title:
            error = _("Give the request a title.")
        elif not any(creative_formats.get(k) for k in format_keys):
            error = _("Choose at least one format.")
        if not error:
            task = services.request_arts(
                workspace,
                request.user,
                title=title,
                description=request.POST.get("description", "").strip(),
                post_caption=request.POST.get("post_caption", "").strip(),
                format_keys=format_keys,
                assignees=services.resolve_assignees(workspace, request.POST.getlist("assignees")),
                due_date=_parse_day(request.POST.get("due_date")),
                idea=idea,
            )
            board_url = f"{reverse('tasks:board', kwargs={'workspace_id': workspace.id})}?task={task.pk}"
            if request.htmx:
                return HttpResponse(status=204, headers={"HX-Redirect": board_url})
            return redirect(board_url)
        selected = set(format_keys)
        chosen_assignees = set(request.POST.getlist("assignees"))
        form = {
            "title": title,
            "description": request.POST.get("description", ""),
            "post_caption": request.POST.get("post_caption", ""),
            "due_date": request.POST.get("due_date", ""),
        }
    else:
        error = ""
        selected = set()
        chosen_assignees = {str(task_settings.art_assignee_id)} if task_settings.art_assignee_id else set()
        due = None
        if idea and idea.target_date:
            due = max(timezone.localdate(), idea.target_date - timedelta(days=task_settings.art_lead_days))
        form = {
            "title": _("Arts: %(title)s") % {"title": idea.title} if idea else "",
            "description": "",
            "post_caption": idea.description if idea else "",
            "due_date": due.isoformat() if due else "",
        }

    return render_art_request_form(
        request,
        form=form,
        selected=selected,
        chosen_assignees=chosen_assignees,
        error=error,
        idea=idea,
    )


def render_art_request_form(
    request,
    *,
    form,
    selected=(),
    chosen_assignees=None,
    error="",
    idea=None,
    action_url="",
    hidden=None,
    source_note="",
):
    """The art request form, in the modal (htmx) or as its own page.

    Other apps reuse it with ``action_url`` (where it posts instead of
    ``tasks:request_arts``), ``hidden`` (name → value pairs posted along) and
    ``source_note`` (the "From …" line); see ``apps.radar.views.offer_art_request``.
    ``chosen_assignees=None`` preselects the workspace's default art assignee.
    """
    from apps.social_accounts.models import PlatformVisibility, SocialAccount

    workspace = request.workspace
    if chosen_assignees is None:
        task_settings = services.art_settings(workspace)
        chosen_assignees = {str(task_settings.art_assignee_id)} if task_settings.art_assignee_id else set()
    accounts = list(
        SocialAccount.objects.for_workspace(workspace.id)
        .publishable()
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .order_by("platform", "account_name")
    )
    visible = [value for value, _label in PlatformVisibility.visible_choices()]
    groups = creative_formats.catalogue(accounts, visible)
    # In the modal (htmx) only the form is swapped in, and an invalid form is
    # answered 200 so htmx swaps it in with its error.
    return render(
        request,
        "tasks/partials/_request_arts_form.html" if request.htmx else "tasks/request_arts.html",
        {
            "workspace": workspace,
            "idea": idea,
            "groups": groups,
            "selected": set(selected),
            "members": services.assignable_members(workspace),
            "chosen_assignees": chosen_assignees,
            "form": form,
            "error": error,
            "first_tab": groups[0]["network"].key if groups else "",
            "action_url": action_url,
            "hidden": (hidden or {}).items(),
            "source_note": source_note,
        },
        status=400 if error and not request.htmx else 200,
    )


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
    """Upload the file for an art item; warns when its proportions don't match the format."""
    from django.core.exceptions import ValidationError

    from apps.billing.storage_usage import WorkspaceStorageExceededError
    from apps.media_library.quotas import StorageQuotaExceededError
    from apps.media_library.services import create_asset, extract_image_metadata
    from apps.media_library.tasks import process_media_asset

    item = _item(request, task_id, item_id)
    uploaded = request.FILES.get("file")
    if not item.is_art or not uploaded:
        return toast_response(tone="error", title=_("Choose the file of the art."))
    workspace = request.workspace
    try:
        asset = create_asset(workspace.organization, workspace, uploaded, request.user)
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
    task = get_object_or_404(Task.objects.select_related("idea", "workspace"), id=task_id, workspace=request.workspace)
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
