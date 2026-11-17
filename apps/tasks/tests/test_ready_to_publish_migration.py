"""Migration 0008 over boards that already exist: the done column still named as the
default becomes "Ready to publish"; one the team renamed keeps its name."""

import importlib

import pytest
from django.apps import apps
from django.test import override_settings

from apps.organizations.models import Organization
from apps.tasks.models import TaskColumn
from apps.workspaces.models import Workspace

migration = importlib.import_module("apps.tasks.migrations.0008_ready_to_publish_column")


@pytest.mark.django_db
@override_settings(LANGUAGE_CODE="en-us")
def test_only_the_default_named_done_column_is_renamed():
    organization = Organization.objects.create(name="Acme")
    seeded = Workspace.objects.create(organization=organization, name="Seeded")
    custom = Workspace.objects.create(organization=organization, name="Custom")
    TaskColumn.objects.create(workspace=seeded, name="Done", is_done=True)
    TaskColumn.objects.create(workspace=seeded, name="Done", position=1)
    TaskColumn.objects.create(workspace=custom, name="Shipped", is_done=True)

    migration.rename_done_columns(apps, None)

    assert sorted(TaskColumn.objects.filter(workspace=seeded).values_list("name", flat=True)) == [
        "Done",
        "Ready to publish",
    ]
    assert TaskColumn.objects.get(workspace=custom).name == "Shipped"
