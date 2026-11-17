"""Migration 0002 over columns that already exist: done columns turn green,
review columns yellow, and the rest keep the default purple."""

import importlib

import pytest
from django.apps import apps

from apps.organizations.models import Organization
from apps.tasks.models import TaskColumn
from apps.workspaces.models import Workspace

migration = importlib.import_module("apps.tasks.migrations.0002_column_tone")


@pytest.mark.django_db
def test_existing_columns_get_the_colour_of_their_role():
    workspace = Workspace.objects.create(organization=Organization.objects.create(name="Acme"), name="Acme Social")
    plain = TaskColumn.objects.create(workspace=workspace, name="Backlog", tone="brand")
    review = TaskColumn.objects.create(workspace=workspace, name="Review", is_review=True, tone="brand")
    done = TaskColumn.objects.create(workspace=workspace, name="Done", is_done=True, tone="brand")

    migration.colour_existing_columns(apps, None)

    tones = dict(TaskColumn.objects.filter(workspace=workspace).values_list("name", "tone"))
    assert tones == {plain.name: "brand", review.name: "warning", done.name: "success"}
