"""The task board: who may see and change it, moving cards, checklist and notifications."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.notifications.models import EventType, Notification
from apps.organizations.models import Organization
from apps.tasks import services
from apps.tasks.models import Task, TaskColumn
from apps.workspaces.models import Workspace

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_english")]

Role = WorkspaceMembership.WorkspaceRole


@pytest.fixture
def _english(settings):
    settings.LANGUAGE_CODE = "en-us"


def _user(email, workspace, role):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.get_or_create(
        user=user, organization=workspace.organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
    )
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user


def _client(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def workspace():
    org = Organization.objects.create(name="Acme")
    return Workspace.objects.create(organization=org, name="Acme Social")


@pytest.fixture
def owner(workspace):
    return _user("owner@example.com", workspace, Role.OWNER)


def _url(name, workspace, **kwargs):
    return reverse(f"tasks:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


def test_first_visit_seeds_the_columns_and_lists_tasks(workspace, owner):
    services.create_task(workspace, owner, title="Write the launch brief")

    body = _client(owner).get(_url("board", workspace)).content.decode()

    assert [c.name for c in TaskColumn.objects.for_workspace(workspace.id)] == [
        "To do",
        "In progress",
        "In review",
        "Done",
    ]
    assert "Write the launch brief" in body


def test_a_client_cannot_open_the_board(workspace):
    client_user = _user("client@example.com", workspace, Role.CLIENT)

    assert _client(client_user).get(_url("board", workspace)).status_code == 403


def test_a_viewer_sees_the_board_but_cannot_change_it(workspace, owner):
    viewer = _user("viewer@example.com", workspace, Role.VIEWER)
    task = services.create_task(workspace, owner, title="Plan")
    client = _client(viewer)

    assert client.get(_url("board", workspace)).status_code == 200
    assert client.post(_url("task_create", workspace), {"title": "Nope"}).status_code == 403
    assert client.post(_url("task_delete", workspace, task_id=task.id)).status_code == 403
    assert Task.objects.filter(pk=task.pk).exists()


def test_tasks_of_another_workspace_are_not_reachable(workspace, owner):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    WorkspaceMembership.objects.create(user=owner, workspace=other, workspace_role=Role.OWNER)
    task = services.create_task(workspace, owner, title="Secret")
    other_column = services.ensure_columns(other)[1]
    client = _client(owner)

    assert client.get(_url("task_detail", other, task_id=task.id)).status_code == 404
    assert client.post(_url("task_update", other, task_id=task.id), {"title": "Hijacked"}).status_code == 404
    # A column of another workspace can't receive this workspace's task.
    assert (
        client.post(
            _url("task_move", workspace, task_id=task.id), {"column": other_column.id, "position": 0}
        ).status_code
        == 404
    )
    task.refresh_from_db()
    assert task.title == "Secret"


def test_moving_to_the_done_column_completes_the_task_and_back_reopens_it(workspace, owner):
    task = services.create_task(workspace, owner, title="Ship")
    todo, _progress, _review, done = services.ensure_columns(workspace)
    client = _client(owner)

    client.post(_url("task_move", workspace, task_id=task.id), {"column": done.id, "position": 0})
    task.refresh_from_db()
    assert task.column == done and task.completed_at is not None

    client.post(_url("task_move", workspace, task_id=task.id), {"column": todo.id, "position": 0})
    task.refresh_from_db()
    assert task.column == todo and task.completed_at is None


def test_moving_renumbers_the_target_column(workspace, owner):
    first = services.create_task(workspace, owner, title="First")
    services.create_task(workspace, owner, title="Second")
    third = services.create_task(workspace, owner, title="Third")
    column = first.column

    _client(owner).post(_url("task_move", workspace, task_id=third.id), {"column": column.id, "position": 0})

    ordered = list(Task.objects.filter(column=column).order_by("position").values_list("title", flat=True))
    assert ordered == ["Third", "First", "Second"]


def test_only_members_who_create_content_can_be_assigned(workspace, owner):
    editor = _user("editor@example.com", workspace, Role.EDITOR)
    viewer = _user("viewer@example.com", workspace, Role.VIEWER)
    outsider = User.objects.create_user(email="out@example.com", password="x")
    task = services.create_task(workspace, owner, title="Design")

    _client(owner).post(
        _url("task_update", workspace, task_id=task.id),
        {"assignees_sent": "1", "assignees": [str(editor.pk), str(viewer.pk), str(outsider.pk)]},
    )

    assert list(task.assignees.all()) == [editor]


def test_a_new_assignee_is_notified_once(workspace, owner):
    editor = _user("editor@example.com", workspace, Role.EDITOR)
    task = services.create_task(workspace, owner, title="Design")
    url = _url("task_update", workspace, task_id=task.id)
    client = _client(owner)

    client.post(url, {"assignees_sent": "1", "assignees": [str(editor.pk)]})
    client.post(url, {"assignees_sent": "1", "assignees": [str(editor.pk), str(owner.pk)]})

    notes = Notification.objects.filter(user=editor, event_type=EventType.TASK_ASSIGNED)
    assert notes.count() == 1
    assert str(task.pk) in notes.get().data["action_url"]
    # Assigning yourself doesn't notify you.
    assert not Notification.objects.filter(user=owner, event_type=EventType.TASK_ASSIGNED).exists()


def test_checklist_add_toggle_and_remove(workspace, owner):
    task = services.create_task(workspace, owner, title="Prepare")
    client = _client(owner)

    body = client.post(_url("item_add", workspace, task_id=task.id), {"text": "Find photos"}).content.decode()
    assert "Find photos" in body
    item = task.checklist.get()

    client.post(_url("item_toggle", workspace, task_id=task.id, item_id=item.id))
    item.refresh_from_db()
    assert item.is_done and item.done_by == owner

    client.post(_url("item_delete", workspace, task_id=task.id, item_id=item.id))
    assert not task.checklist.exists()


def test_deleting_a_column_sends_its_tasks_to_the_column_beside_it(workspace, owner):
    todo, progress, _review, _done = services.ensure_columns(workspace)
    kept = services.create_task(workspace, owner, title="Already there", column=todo)
    first = services.create_task(workspace, owner, title="First", column=progress)
    second = services.create_task(workspace, owner, title="Second", column=progress)
    services.move_task(second, progress, 1, owner)

    response = _client(owner).post(_url("column_delete", workspace, column_id=progress.id))

    assert "showToast" in response["HX-Trigger"]
    assert not TaskColumn.objects.filter(pk=progress.id).exists()
    ordered = list(Task.objects.filter(column=todo).order_by("position").values_list("pk", flat=True))
    assert ordered == [kept.pk, first.pk, second.pk]


def test_the_first_column_hands_its_tasks_to_the_next_and_the_last_column_stays(workspace, owner):
    todo, progress, review, done = services.ensure_columns(workspace)
    task = services.create_task(workspace, owner, title="Move me", column=todo)

    _client(owner).post(_url("column_delete", workspace, column_id=todo.id))
    task.refresh_from_db()
    assert task.column == progress

    for column in (progress, review):
        _client(owner).post(_url("column_delete", workspace, column_id=column.id))
    response = _client(owner).post(_url("column_delete", workspace, column_id=done.id))

    assert "showToast" in response["HX-Trigger"]
    assert TaskColumn.objects.filter(pk=done.id).exists()
    task.refresh_from_db()
    assert task.column == done and task.completed_at is not None


def test_a_column_takes_a_colour_only_from_the_palette(workspace, owner):
    column = services.ensure_columns(workspace)[0]
    client = _client(owner)

    client.post(_url("column_rename", workspace, column_id=column.id), {"tone": "success"})
    client.post(_url("column_rename", workspace, column_id=column.id), {"tone": "url(evil)"})

    column.refresh_from_db()
    assert column.tone == "success" and column.name == "To do"


def test_another_workspace_cannot_delete_or_recolour_a_column(workspace, owner):
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    foreign = services.ensure_columns(other)[0]
    client = _client(owner)

    assert client.post(_url("column_delete", workspace, column_id=foreign.id)).status_code == 404
    assert client.post(_url("column_rename", workspace, column_id=foreign.id), {"tone": "error"}).status_code == 404
    foreign.refresh_from_db()
    assert foreign.tone == "brand"


@override_settings(LANGUAGE_CODE="en-us")
def test_due_reminder_goes_out_once_per_due_date(workspace, owner):
    editor = _user("editor@example.com", workspace, Role.EDITOR)
    today = date(2026, 5, 4)
    task = services.create_task(
        workspace, owner, title="Mother's Day arts", due_date=today + timedelta(days=1), assignees=[editor]
    )
    services.create_task(workspace, owner, title="Later", due_date=today + timedelta(days=5), assignees=[editor])

    assert services.send_due_reminders(today) == 1
    assert services.send_due_reminders(today) == 0

    task.due_date = today + timedelta(days=2)
    task.save()
    assert services.send_due_reminders(today + timedelta(days=1)) == 1


def test_the_filter_bar_names_the_active_filters_and_keeps_the_other_one(workspace, owner):
    services.create_task(workspace, owner, title="Urgent one", priority=Task.Priority.HIGH, assignees=[owner])
    services.create_task(workspace, owner, title="Calm one", priority=Task.Priority.LOW, assignees=[owner])

    body = (
        _client(owner)
        .get(_url("board", workspace), {"assignee": "me", "priority": "high", "task": "x"})
        .content.decode()
    )

    assert "Urgent one" in body and "Calm one" not in body
    assert "Priority: High" in body
    # Each pill clears its own filter, keeps the other and drops the card to open.
    assert 'href="?priority=high"' in body
    assert 'href="?assignee=me"' in body


def test_an_unknown_priority_is_ignored(workspace, owner):
    services.create_task(workspace, owner, title="Any task")

    body = _client(owner).get(_url("board", workspace), {"priority": "urgent"}).content.decode()

    assert "Any task" in body and "Priority: " not in body
