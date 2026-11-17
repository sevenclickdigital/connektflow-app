"""Migration 0006 over boards that already exist: "Changes requested" joins them
right before the done column, once."""

import importlib

import pytest
from django.apps import apps
from django.test import override_settings

from apps.organizations.models import Organization
from apps.tasks.models import TaskColumn
from apps.workspaces.models import Workspace

migration = importlib.import_module("apps.tasks.migrations.0006_changes_requested_column")


def _workspace(name):
    return Workspace.objects.create(organization=Organization.objects.create(name=name), name=name)


def _names(workspace):
    return list(TaskColumn.objects.filter(workspace=workspace).order_by("position").values_list("name", flat=True))


@pytest.mark.django_db
@override_settings(LANGUAGE_CODE="en-us")
def test_the_column_goes_right_before_done_on_every_existing_board():
    seeded = _workspace("Seeded")
    for position, (name, review, done) in enumerate(
        [("To do", False, False), ("In progress", False, False), ("In review", True, False), ("Done", False, True)]
    ):
        TaskColumn.objects.create(workspace=seeded, name=name, position=position, is_review=review, is_done=done)
    no_done = _workspace("No done")
    TaskColumn.objects.create(workspace=no_done, name="Ideas", position=0)

    migration.add_changes_requested(apps, None)

    assert _names(seeded) == ["To do", "In progress", "In review", "Changes requested", "Done"]
    added = TaskColumn.objects.get(workspace=seeded, name="Changes requested")
    assert added.tone == "error" and added.is_changes_requested
    assert _names(no_done) == ["Ideas", "Changes requested"]


@pytest.mark.django_db
@override_settings(LANGUAGE_CODE="en-us")
def test_a_board_that_has_it_already_is_left_alone():
    workspace = _workspace("Has it")
    TaskColumn.objects.create(workspace=workspace, name="changes requested", position=0)
    TaskColumn.objects.create(workspace=workspace, name="Done", position=1, is_done=True)

    migration.add_changes_requested(apps, None)

    assert _names(workspace) == ["changes requested", "Done"]
    assert TaskColumn.objects.get(workspace=workspace, name="changes requested").is_changes_requested
