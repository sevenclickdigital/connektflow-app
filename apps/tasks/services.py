"""Business rules of the task board. Views parse input and call these."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import override

from apps.members.models import WorkspaceMembership

from .models import Task, TaskChecklist, TaskChecklistItem, TaskColumn, TaskComment


def ensure_columns(workspace) -> list[TaskColumn]:
    """The board's columns, seeding the default five the first time.

    Column names are data (users rename them), so they are written once, in
    the site's language rather than that of whoever happens to open the board
    first, which would leave a Portuguese team with English columns.
    """
    columns = list(TaskColumn.objects.for_workspace(workspace.id))
    if columns:
        return columns
    tone = TaskColumn.Tone
    with override(settings.LANGUAGE_CODE):
        defaults = [
            (_("To do"), tone.BRAND, False, False, False),
            (_("In progress"), tone.INFO, False, False, False),
            (_("In review"), tone.WARNING, True, False, False),
            (_("Changes requested"), tone.ERROR, False, True, False),
            (_("Ready to publish"), tone.SUCCESS, False, False, True),
        ]
    return [
        TaskColumn.objects.create(
            workspace=workspace,
            name=name,
            tone=colour,
            position=pos,
            is_review=review,
            is_changes_requested=changes,
            is_done=done,
        )
        for pos, (name, colour, review, changes, done) in enumerate(defaults)
    ]


def create_column(workspace, name, *, after=None) -> TaskColumn:
    """A new column named ``name``, right after ``after`` (a column of this board), or at the end.

    The columns to its right move one place along so the order stays a plain 0..n sequence.
    """
    columns = ensure_columns(workspace)
    index = next((i + 1 for i, c in enumerate(columns) if after is not None and c.pk == after.pk), len(columns))
    with transaction.atomic():
        column = TaskColumn(workspace=workspace, name=name[:100])
        columns.insert(index, column)
        for position, item in enumerate(columns):
            item.position = position
        column.save()
        TaskColumn.objects.bulk_update([c for c in columns if c.pk != column.pk], ["position"])
    return column


def media_folder(workspace):
    """The media library folder the task board files its uploads in ("Tasks", in the site's language).

    Written once in the site's language, like the default column names, so every
    member finds the same folder whatever language they use.
    """
    from apps.media_library.services import top_level_folder

    with override(settings.LANGUAGE_CODE):
        name = str(_("Tasks"))
    return top_level_folder(workspace, name)


def delete_column(column, actor) -> TaskColumn | None:
    """Delete ``column``; its tasks go, in order, to the end of the column beside it.

    The neighbour is the column on its left, or on its right when it is the
    first. Returns that column, or None when the column was the board's only
    one (nothing is deleted then: the board needs a column).
    """
    columns = list(TaskColumn.objects.for_workspace(column.workspace_id))
    others = [c for c in columns if c.pk != column.pk]
    if not others:
        return None
    index = next(i for i, c in enumerate(columns) if c.pk == column.pk)
    target = columns[index - 1] if index > 0 else others[0]
    with transaction.atomic():
        for task in Task.objects.filter(column=column).order_by("position", "-created_at"):
            move_task(task, target, _next_position(target), actor)
        column.delete()
    return target


def assignable_members(workspace):
    """Users who can be made responsible for a task: members who can create content."""
    memberships = (
        WorkspaceMembership.objects.filter(workspace=workspace, user__is_active=True)
        .select_related("user", "custom_role")
        .order_by("user__name", "user__email")
    )
    return [m.user for m in memberships if m.effective_permissions.get("create_posts")]


MAX_TASK_TAGS = 5  # networks ignore hashtags past about five (as composer.services.MAX_IDEA_TAGS)
MAX_TAG_LENGTH = 50


def clean_tags(raw) -> list[str]:
    """Tag names as typed: trimmed, without a leading "#", no blanks or repeats, at most MAX_TASK_TAGS."""
    tags: list[str] = []
    for value in raw:
        name = str(value).strip().lstrip("#").strip()[:MAX_TAG_LENGTH]
        if name and name not in tags:
            tags.append(name)
    return tags[:MAX_TASK_TAGS]


def tag_suggestions(workspace) -> list[str]:
    """The brand's saved tags (shared with ideas and posts), by name."""
    from apps.composer.models import Tag

    return list(Tag.objects.for_workspace(workspace.id).order_by("name").values_list("name", flat=True))


def set_tags(task, raw) -> None:
    """Replace the task's tags, saving new names in the brand's tag list."""
    from apps.composer.services import ensure_tags

    task.tags = clean_tags(raw)
    task.save(update_fields=["tags", "updated_at"])
    ensure_tags(task.workspace, task.tags)


MAX_CHECKLISTS = 10
MAX_CHECKLIST_ITEMS = 50


def clean_checklists(raw) -> list[dict]:
    """Named checklists as posted (a list of ``{"title", "items"}``), trimmed and capped.

    An item is its text, or ``{"text", "accepts_file", "width", "height"}`` when its
    settings were set (the new task window's gear; the size cleaned by clean_item_size).
    A checklist without a title keeps a numbered "Checklist N"; one without items is kept
    (the team fills it later). Anything that isn't that shape is dropped.
    """
    checklists: list[dict] = []
    for spec in raw if isinstance(raw, list) else []:
        if not isinstance(spec, dict):
            continue
        items = [item for item in (_clean_item(raw_item) for raw_item in spec.get("items") or []) if item]
        title = str(spec.get("title") or "").strip()[:120] or _("Checklist %(number)s") % {
            "number": len(checklists) + 1
        }
        checklists.append({"title": title, "items": items[:MAX_CHECKLIST_ITEMS]})
    return checklists[:MAX_CHECKLISTS]


def _clean_item(raw):
    """One posted checklist item (clean_checklists): its text, or a dict with its settings."""
    if isinstance(raw, str | int):
        return str(raw).strip()[:255]
    if not isinstance(raw, dict):
        return ""
    text = str(raw.get("text") or "").strip()[:255]
    if not text:
        return ""
    width, height = clean_item_size(raw.get("width"), raw.get("height"))
    accepts_file = bool(raw.get("accepts_file"))
    if not (accepts_file or width):
        return text
    return {"text": text, "accepts_file": accepts_file, "width": width, "height": height}


def add_checklist(task, title) -> TaskChecklist:
    top = task.checklists.aggregate(top=Max("position"))["top"]
    return TaskChecklist.objects.create(task=task, title=title.strip()[:120], position=0 if top is None else top + 1)


def add_item(task, text, checklist=None) -> TaskChecklistItem:
    """A to-do at the end of the task's items, in ``checklist`` (one of the task's) or outside any."""
    top = task.checklist.aggregate(top=Max("position"))["top"]
    return TaskChecklistItem.objects.create(
        task=task, checklist=checklist, text=text.strip()[:255], position=0 if top is None else top + 1
    )


def checklist_groups(task, items) -> list[dict]:
    """The task's items for its window: the untitled ones first (when there are any or no
    named checklist), then each named checklist with its own, each with how many are done.
    """
    named = list(task.checklists.all())
    loose = [i for i in items if i.checklist_id is None]
    groups: list[dict] = []
    if loose or not named:
        groups.append({"checklist": None, "items": loose})
    for checklist in named:
        groups.append({"checklist": checklist, "items": [i for i in items if i.checklist_id == checklist.pk]})
    for group in groups:
        group["done"] = sum(1 for i in group["items"] if i.is_done)
    return groups


MAX_ITEM_SIDE = 10000


def clean_item_size(width, height) -> tuple[int | None, int | None]:
    """An item's file size as typed: both sides positive whole pixels up to MAX_ITEM_SIDE,
    or (None, None) when either side is missing or isn't one."""

    def side(value):
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return number if 0 < number <= MAX_ITEM_SIDE else None

    width, height = side(width), side(height)
    return (width, height) if width and height else (None, None)


def configure_item(item, *, accepts_file, width, height) -> None:
    """The item's settings: whether it asks for a file, and the size that file should have."""
    item.accepts_file = bool(accepts_file)
    item.width, item.height = clean_item_size(width, height)
    item.save(update_fields=["accepts_file", "width", "height"])


MAX_REFERENCES = 10


def set_references(task, asset_ids) -> None:
    """The art request's reference images: the workspace's images among ``asset_ids``, in
    the order given, up to MAX_REFERENCES; ids of other workspaces or files are dropped."""
    from apps.media_library.models import MediaAsset

    wanted = []
    for raw in asset_ids:
        try:
            value = str(uuid.UUID(str(raw)))
        except ValueError:
            continue
        if value not in wanted:
            wanted.append(value)
    found = MediaAsset.objects.filter(
        workspace=task.workspace, media_type=MediaAsset.MediaType.IMAGE, id__in=wanted[:MAX_REFERENCES]
    ).values_list("id", flat=True)
    task.references.set(list(found))


class CommentNotAllowedError(Exception):
    """Comments are written while the task is in review."""


def add_comment(checklist, author, body):
    """A comment on one of a task's checklists, while the task is in review; the body is
    trimmed and capped (TaskComment.MAX_LENGTH). Returns it, or None when it was blank."""
    if not checklist.task.column.is_review:
        raise CommentNotAllowedError
    body = (body or "").strip()[: TaskComment.MAX_LENGTH]
    if not body:
        return None
    return TaskComment.objects.create(checklist=checklist, author=author, body=body)


def resolve_assignees(workspace, raw_ids) -> list:
    """Keep only ids of users who may be assigned in this workspace."""
    wanted = {str(i) for i in raw_ids if i}
    return [u for u in assignable_members(workspace) if str(u.pk) in wanted]


def board_columns(workspace, *, assignee_id=None, priority=None):
    """Columns with their (filtered) tasks, checklist progress and assignees."""
    columns = ensure_columns(workspace)
    tasks = (
        Task.objects.for_workspace(workspace.id)
        .select_related("idea")
        .prefetch_related("assignees")
        .annotate(
            items_total=Count("checklist", distinct=True),
            items_done=Count("checklist", filter=Q(checklist__is_done=True), distinct=True),
        )
        .order_by("position", "-created_at")
    )
    if assignee_id:
        tasks = tasks.filter(assignees__id=assignee_id)
    if priority in Task.Priority.values:
        tasks = tasks.filter(priority=priority)

    by_column: dict = {c.id: [] for c in columns}
    for task in tasks:
        by_column.setdefault(task.column_id, []).append(task)
    for column in columns:
        column.task_list = by_column.get(column.id, [])
    return columns


def task_detail_queryset(workspace):
    return (
        Task.objects.for_workspace(workspace.id)
        .select_related("column", "idea", "post", "calendar_event", "created_by")
        .prefetch_related(
            "assignees",
            "references",
            Prefetch(
                "checklists",
                queryset=TaskChecklist.objects.prefetch_related(
                    Prefetch("comments", queryset=TaskComment.objects.select_related("author"))
                ),
            ),
            Prefetch("checklist", queryset=TaskChecklistItem.objects.select_related("media_asset", "done_by")),
        )
    )


def _next_position(column) -> int:
    top = Task.objects.filter(column=column).aggregate(top=Max("position"))["top"]
    return 0 if top is None else top + 1


@transaction.atomic
def create_task(
    workspace,
    actor,
    *,
    title,
    column=None,
    description="",
    briefing="",
    post_caption="",
    channel_formats=None,
    priority=Task.Priority.MEDIUM,
    due_date=None,
    assignees=(),
    tags=(),
    checklists=(),
    idea=None,
    calendar_event=None,
    items=(),
) -> Task:
    """Create a task at the bottom of ``column`` (the first column by default).

    ``items`` are dicts of ``TaskChecklistItem`` fields, in order, outside any named
    checklist; ``checklists`` are ``{"title", "items": [text, …]}`` (clean_checklists).
    ``channel_formats`` are the post's channels, already cleaned
    (``composer.services.clean_channel_formats``).
    """
    column = column or ensure_columns(workspace)[0]
    task = Task.objects.create(
        workspace=workspace,
        column=column,
        position=_next_position(column),
        title=title[:255],
        description=description,
        briefing=briefing,
        post_caption=post_caption,
        channel_formats=channel_formats or {},
        priority=priority,
        due_date=due_date,
        tags=clean_tags(tags),
        created_by=actor,
        idea=idea,
        calendar_event=calendar_event,
        completed_at=timezone.now() if column.is_done else None,
    )
    rows = [TaskChecklistItem(task=task, position=pos, **item) for pos, item in enumerate(items)]
    for index, spec in enumerate(checklists):
        checklist = TaskChecklist.objects.create(task=task, title=spec["title"], position=index)
        # An item is its text, or a dict of TaskChecklistItem fields (an art item's format and size).
        rows += [
            TaskChecklistItem(
                task=task,
                checklist=checklist,
                position=len(rows) + offset,
                **(item if isinstance(item, dict) else {"text": item}),
            )
            for offset, item in enumerate(spec["items"])
        ]
    TaskChecklistItem.objects.bulk_create(rows)
    if task.tags:
        from apps.composer.services import ensure_tags

        ensure_tags(workspace, task.tags)
    if assignees:
        task.assignees.set(assignees)
        transaction.on_commit(lambda: notify_assigned(task, assignees, actor))
    return task


def set_assignees(task, users, actor) -> None:
    """Replace who is responsible; only the newly added people are notified."""
    before = set(task.assignees.values_list("pk", flat=True))
    task.assignees.set(users)
    added = [u for u in users if u.pk not in before]
    if added:
        notify_assigned(task, added, actor)


@transaction.atomic
def move_task(task, column, position, actor) -> None:
    """Put ``task`` at ``position`` of ``column`` and renumber that column."""
    moved_column = task.column_id != column.id
    siblings = list(Task.objects.filter(column=column).exclude(pk=task.pk).order_by("position", "-created_at"))
    position = max(0, min(position, len(siblings)))
    siblings.insert(position, task)
    for index, sibling in enumerate(siblings):
        sibling.position = index
    task.column = column
    if moved_column:
        task.completed_at = timezone.now() if column.is_done else None
    Task.objects.bulk_update([t for t in siblings if t.pk != task.pk], ["position"])
    task.save(update_fields=["column", "position", "completed_at", "updated_at"])


def set_item_done(item, done, actor) -> None:
    item.is_done = done
    item.done_by = actor if done else None
    item.done_at = timezone.now() if done else None
    item.save(update_fields=["is_done", "done_by", "done_at"])
    if done:
        start_work(item.task, actor)


def start_work(task, actor) -> bool:
    """Move a task from the board's first column to the next one once an item is ticked.

    The work has started: the card goes from "To do" to "In progress" (whatever the
    team named them), unless the next column is the review or the done one. Tasks
    in any other column stay where they are. Returns whether it moved.
    """
    following = _work_column(task)
    if following is None:
        return False
    move_task(task, following, _next_position(following), actor)
    return True


def _work_column(task):
    """Where a task in the board's first column goes once its work starts: the next
    column, when it is a working one (not review, changes requested or done); else None."""
    columns = list(TaskColumn.objects.for_workspace(task.workspace_id).order_by("position", "id"))
    if len(columns) < 2 or task.column_id != columns[0].id:
        return None
    following = columns[1]
    if following.is_review or following.is_changes_requested or following.is_done:
        return None
    return following


def review_column(workspace_id):
    """The board's review column, or None."""
    return TaskColumn.objects.for_workspace(workspace_id).filter(is_review=True).first()


def stage_actions(task) -> list[str]:
    """The stages a task can be moved to from its window, by its column: in the first
    column, start the work ("start", to the next one); once working, to review; in review,
    back for changes or on to done; done, none. A stage whose column the board doesn't
    have is left out."""
    column = task.column
    if column.is_done:
        return []
    if _work_column(task) is not None:
        return ["start"]
    have = TaskColumn.objects.for_workspace(task.workspace_id)
    if column.is_review:
        wanted = [("changes", have.filter(is_changes_requested=True)), ("done", have.filter(is_done=True))]
    else:
        wanted = [("review", have.filter(is_review=True))]
    return [stage for stage, columns in wanted if columns.exists()]


def move_to_stage(task, stage, actor) -> bool:
    """Move a task to the column of ``stage`` ("start", "review", "changes" or "done") when its window offers it
    (stage_actions). Review goes through send_to_review. Returns whether it moved."""
    if stage not in stage_actions(task):
        return False
    if stage == "start":
        return start_work(task, actor)
    if stage == "review":
        return send_to_review(task, actor)
    flag = {"changes": "is_changes_requested", "done": "is_done"}[stage]
    column = TaskColumn.objects.for_workspace(task.workspace_id).filter(**{flag: True}).first()
    move_task(task, column, _next_position(column), actor)
    return True


def send_to_review(task, actor) -> bool:
    """Move a task to the review column and tell whoever created it.

    A task already in review or done stays where it is, as one on a board without
    a review column. Returns whether it moved.
    """
    if task.column.is_review or task.column.is_done:
        return False
    review = review_column(task.workspace_id)
    if review is None:
        return False
    move_task(task, review, _next_position(review), actor)
    if task.created_by_id and task.created_by != actor:
        notify_ready_for_review(task)
    return True


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def propose_post_date(task, when) -> bool:
    """Record when the task's draft post should go out (``Post.proposed_publish_at``).

    That is what places the draft on the calendar and pre-fills the composer's
    schedule. Ignored once the post is scheduled or published: from then on the
    composer owns the date. Returns whether anything changed.
    """
    post = task.post
    if post is None or post.scheduled_at or post.published_at:
        return False
    post.proposed_publish_at = when
    post.save(update_fields=["proposed_publish_at", "updated_at"])
    return True


def task_url(task) -> str:
    path = reverse("tasks:board", kwargs={"workspace_id": task.workspace_id})
    return f"{settings.APP_URL.rstrip('/')}{path}?task={task.pk}"


def notify_assigned(task, users, actor) -> None:
    from apps.notifications.engine import notify
    from apps.notifications.models import EventType

    who = actor.display_name if actor else ""
    for user in users:
        if actor and user.pk == actor.pk:
            continue
        with override(settings.LANGUAGE_CODE):
            notify(
                user,
                EventType.TASK_ASSIGNED,
                title=_("New task: %(title)s") % {"title": task.title},
                body=_("%(who)s made you responsible for this task.") % {"who": who} if who else "",
                data={"action_url": task_url(task), "task_id": str(task.pk), "workspace_id": str(task.workspace_id)},
            )


def notify_ready_for_review(task) -> None:
    from apps.notifications.engine import notify
    from apps.notifications.models import EventType

    with override(settings.LANGUAGE_CODE):
        notify(
            task.created_by,
            EventType.TASK_READY_FOR_REVIEW,
            title=_("Ready for review: %(title)s") % {"title": task.title},
            body=_("The task was sent for review."),
            data={"action_url": task_url(task), "task_id": str(task.pk), "workspace_id": str(task.workspace_id)},
        )


def send_due_reminders(today=None) -> int:
    """Remind the people responsible for open tasks due tomorrow. Once per due date."""
    from datetime import timedelta

    from apps.notifications.engine import notify
    from apps.notifications.models import EventType

    today = today or timezone.localdate()
    tomorrow = today + timedelta(days=1)
    due = (
        Task.objects.filter(due_date=tomorrow, completed_at__isnull=True)
        .exclude(due_reminder_sent_on=tomorrow)
        .prefetch_related("assignees")
    )
    sent = 0
    for task in due:
        with override(settings.LANGUAGE_CODE):
            for user in task.assignees.all():
                notify(
                    user,
                    EventType.TASK_DUE_SOON,
                    title=_("Due tomorrow: %(title)s") % {"title": task.title},
                    data={"action_url": task_url(task), "task_id": str(task.pk)},
                )
                sent += 1
        task.due_reminder_sent_on = tomorrow
        task.save(update_fields=["due_reminder_sent_on"])
    return sent


# ---------------------------------------------------------------------------
# Art requests
# ---------------------------------------------------------------------------


def art_settings(workspace):
    from .models import TaskSettings

    task_settings, _created = TaskSettings.objects.get_or_create(workspace=workspace)
    return task_settings


def art_due_date(workspace, content_date):
    """When the arts for content dated ``content_date`` are due: the brand's lead days
    before it (TaskSettings.art_lead_days), never before today; None without a date."""
    from datetime import timedelta

    if not content_date:
        return None
    return max(timezone.localdate(), content_date - timedelta(days=art_settings(workspace).art_lead_days))


def default_art_assignees(workspace) -> list:
    """Who an art request goes to when nobody is picked: the brand's art assignee, while a member."""
    default = art_settings(workspace).art_assignee
    return [default] if default and default in assignable_members(workspace) else []


def blank_prefill(**values) -> dict:
    """What the new task window starts from (tasks.views.render_task_window): its texts,
    due date, tags, the post's channels, reference images, the people responsible, the
    two switches ("Request arts", "Create a post") and the idea it comes from."""
    prefill: dict = {
        "title": "",
        "description": "",
        "briefing": "",
        "post_caption": "",
        "due_date": None,
        "tags": [],
        "channels": {},
        "references": [],
        "assignees": [],
        "wants_art": False,
        "wants_post": False,
        "idea": None,
    }
    prefill.update(values)
    prefill["title"] = str(prefill["title"])[:255]
    prefill["tags"] = clean_tags(prefill["tags"])
    return prefill


def material_prefill(workspace, **values) -> dict:
    """A piece of content sent to the board to be made: both switches on, so the arts are
    asked for and the post they become is set, with the brand's art assignee."""
    return blank_prefill(wants_art=True, wants_post=True, assignees=default_art_assignees(workspace), **values)


def prefill_from_idea(idea) -> dict:
    """An idea sent to the board: its title, its text as the post's caption (never the
    briefing), its tags and channels, its images as references, the arts due the lead
    days before its content date."""
    from apps.composer.services import clean_channel_formats
    from apps.media_library.models import MediaAsset

    images = [
        attachment.media_asset
        for attachment in sorted(idea.media_attachments.all(), key=lambda a: a.position)
        if attachment.media_asset.media_type == MediaAsset.MediaType.IMAGE
    ]
    return material_prefill(
        idea.workspace,
        title=idea.title,
        post_caption=idea.description,
        due_date=art_due_date(idea.workspace, idea.target_date),
        tags=idea.tags,
        channels=clean_channel_formats(idea.workspace, idea.channel_formats),
        references=images[:10],
        idea=idea,
    )


def prefill_from_template(workspace, template, variation) -> dict:
    """A template sent to the board: its title, the text of the version chosen as the
    caption, its category as the tag and the brand's accounts on its networks."""
    from apps.composer.services import channel_accounts, template_account_formats, template_text

    return material_prefill(
        workspace,
        title=template.title,
        post_caption=template_text(template, variation),
        tags=[str(template.get_category_display())],
        channels=template_account_formats(template.network_formats, channel_accounts(workspace)),
    )


def prefill_from_date(workspace, *, title, day, brief="", tags=()) -> dict:
    """A special date (or one of its content suggestions) sent to the board: its name, what
    it is about as the briefing for the arts, its suggested tags, due the lead days before it."""
    return material_prefill(
        workspace,
        title=str(title).strip(),
        briefing=str(brief).strip()[:2000],
        due_date=art_due_date(workspace, day),
        tags=list(tags),
    )


def prefill_for_day(workspace, day) -> dict:
    """A task for a day of the calendar: due that day (today when it is past or blank)."""
    today = timezone.localdate()
    return blank_prefill(due_date=max(today, day) if day else None)


def request_arts(
    workspace,
    actor,
    *,
    title,
    format_keys,
    description="",
    briefing="",
    post_caption="",
    channel_formats=None,
    assignees=(),
    due_date=None,
    idea=None,
    extra_items=(),
    checklists=(),
    references=(),
    column=None,
    priority=Task.Priority.MEDIUM,
    tags=(),
) -> Task:
    """One task with a checklist per network ("Instagram", "Facebook"…), each holding an
    item per requested format of that network, in catalogue order; then the task's own
    ``checklists`` (clean_checklists) and, when ``extra_items`` (other to-dos typed with
    the request) are given, an "Other to-dos" checklist with them.

    ``references`` are media library ids of reference images (set_references).
    Unknown keys are dropped; at least one known format is required. "Other to-dos" is
    written in the site's language, like the default column names. ``briefing`` says how
    the arts should be made; ``column`` (the first one by default), ``description``,
    ``priority`` and ``tags`` are those of any task.
    """
    lists = _art_checklists(format_keys)
    if not lists:
        raise ValueError("No known format requested")
    # One checklist per network; then the task's own checklists and "Other to-dos".
    lists += list(checklists)
    extras = [str(text).strip()[:255] for text in extra_items if str(text).strip()][:MAX_CHECKLIST_ITEMS]
    if extras:
        with override(settings.LANGUAGE_CODE):
            lists.append({"title": str(_("Other to-dos")), "items": extras})
    if not assignees:
        assignees = default_art_assignees(workspace)
    task = create_task(
        workspace,
        actor,
        title=title,
        description=description,
        briefing=briefing,
        post_caption=post_caption,
        channel_formats=channel_formats,
        due_date=due_date,
        assignees=list(assignees),
        idea=idea,
        calendar_event=idea.calendar_event if idea else None,
        checklists=lists,
        column=column,
        priority=priority,
        tags=tags,
    )
    if references:
        set_references(task, references)
    return task


def _art_checklists(format_keys) -> list[dict]:
    """One checklist per network (``{"title", "items"}``), in catalogue order, with an art
    item per known format of ``format_keys``, named by the format."""
    from . import creative_formats

    wanted = set(format_keys)
    lists: list[dict] = []
    for fmt in creative_formats.FORMATS:
        if fmt.key not in wanted:
            continue
        label = str(creative_formats.network(fmt.network).label)
        if not lists or lists[-1]["title"] != label:
            lists.append({"title": label, "items": []})
        lists[-1]["items"].append(
            {"text": str(fmt.label), "format_key": fmt.key, "width": fmt.width, "height": fmt.height}
        )
    return lists


@transaction.atomic
def add_arts(task, format_keys) -> int:
    """Ask for more arts on an existing task: the formats it doesn't have yet join the
    checklist of their network (a new one when the task has none). Returns how many."""
    have = set(task.checklist.exclude(format_key="").values_list("format_key", flat=True))
    lists = _art_checklists([key for key in format_keys if key not in have])
    added = 0
    for spec in lists:
        checklist = task.checklists.filter(title=spec["title"]).first() or add_checklist(task, spec["title"])
        top = task.checklist.aggregate(top=Max("position"))["top"]
        start = 0 if top is None else top + 1
        TaskChecklistItem.objects.bulk_create(
            TaskChecklistItem(task=task, checklist=checklist, position=start + offset, **item)
            for offset, item in enumerate(spec["items"])
        )
        added += len(spec["items"])
    return added


def deliver_art(item, asset, actor) -> bool:
    """Attach the delivered file to an art item and tick it.

    Returns whether the file's proportions match the format's. A mismatch is
    reported, never refused: the designer may have a reason (a safe-area
    variant), and the reviewer sees the warning on the card.
    """
    from . import creative_formats

    item.media_asset = asset
    item.is_done = True
    item.done_by = actor
    item.done_at = timezone.now()
    item.save(update_fields=["media_asset", "is_done", "done_by", "done_at"])
    start_work(item.task, actor)
    return creative_formats.ratio_matches(item.width or 0, item.height or 0, asset.width, asset.height)


def remove_art(item) -> None:
    """Take the delivered file off an art item, which goes back to waiting for one.

    The file stays in the media library: it may already be in a post.
    """
    item.media_asset = None
    item.is_done = False
    item.done_by = None
    item.done_at = None
    item.save(update_fields=["media_asset", "is_done", "done_by", "done_at"])


class NothingToPublishError(Exception):
    """The task has no delivered art that this workspace's accounts can publish."""


@transaction.atomic
def create_post_from_task(task, actor):
    """A draft post with each delivered art in the format it was made for.

    It goes out with the task's ``post_caption``; the description and the briefing
    are for the team and never become the caption. The first delivered feed art is the post's media; every other delivered
    art stands in for it on its own format (a Story, a Reel, another network's
    feed) through the same per-format media the composer's crop uses. Formats
    the account can't publish and art-only pieces are left out, and so are the
    channels and formats outside the task's ``channel_formats`` when it has any.
    """
    from apps.composer.models import PlatformPost, Post, PostMedia
    from apps.social_accounts.models import SocialAccount

    from . import creative_formats

    delivered = []
    for item in task.checklist.select_related("media_asset").order_by("position"):
        fmt = creative_formats.get(item.format_key)
        if fmt and fmt.is_publishable and item.media_asset_id:
            delivered.append((fmt, item.media_asset))
    if not delivered:
        raise NothingToPublishError

    accounts = list(
        SocialAccount.objects.for_workspace(task.workspace_id)
        .publishable()
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
    )
    wanted = task.channel_formats or {}
    # (account, publish format) → art, first art per pair in checklist order.
    plan: dict = {}
    for fmt, asset in delivered:
        for account in accounts:
            if (
                account.platform in creative_formats.network(fmt.network).platforms
                and fmt.publish_format in account.formats
                and (not wanted or fmt.publish_format in wanted.get(str(account.id), ()))
            ):
                plan.setdefault((account, fmt.publish_format), asset)
    if not plan:
        raise NothingToPublishError

    primary = next((a for f, a in delivered if f.publish_format == "default"), delivered[0][1])
    idea = task.idea
    post = Post.objects.create(
        workspace=task.workspace,
        author=actor,
        title=(idea.title if idea else task.title)[:255],
        caption=task.post_caption,
        tags=list(idea.tags or []) if idea else [],
    )
    PostMedia.objects.create(post=post, media_asset=primary, position=0)

    rows = []
    for (account, publish_format), asset in plan.items():
        own_media = None
        if asset.pk != primary.pk and asset.derived_from_id in (None, primary.pk):
            if asset.derived_from_id is None:
                asset.derived_from = primary
                asset.save(update_fields=["derived_from", "updated_at"])
            own_media = [str(asset.pk)]
        rows.append(
            PlatformPost(post=post, social_account=account, format=publish_format, platform_specific_media=own_media)
        )
    PlatformPost.objects.bulk_create(rows)

    task.post = post
    task.save(update_fields=["post", "updated_at"])
    if idea and idea.post_id is None:
        idea.post = post
        idea.save(update_fields=["post", "updated_at"])
    return post
