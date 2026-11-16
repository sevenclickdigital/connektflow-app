"""The migration that renames the columns of the idea board, over boards
that already exist."""

import importlib

import pytest
from django.apps import apps as global_apps

from apps.composer.models import Idea, IdeaGroup
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

migration = importlib.import_module("apps.composer.migrations.0030_rename_first_idea_column")

ORIGINAL = ("Sem responsável", "A fazer", "Em andamento", "Pronto")


def _board(names=ORIGINAL):
    workspace = Workspace.objects.create(organization=Organization.objects.create(name="Org"), name="Brand")
    return [IdeaGroup.objects.create(workspace=workspace, name=name, position=i) for i, name in enumerate(names)]


def _names(columns):
    return [IdeaGroup.objects.get(pk=c.pk).name for c in columns]


@pytest.mark.django_db
def test_a_board_with_its_original_names_gets_the_new_ones():
    board = _board()
    english = _board(("Unassigned", "To Do", "In Progress", "Done"))

    migration.rename(global_apps, None)

    assert _names(board) == _names(english) == ["Brainstorming", "Em avaliação", "Aprovadas", "Descartadas"]


@pytest.mark.django_db
def test_a_column_the_brand_renamed_or_moved_is_left_alone():
    renamed = _board(("Ideias soltas", "A fazer", "Em andamento", "Pronto"))
    # The original names, but not where they were created.
    shuffled = _board(("A fazer", "Sem responsável", "Pronto", "Em andamento"))

    migration.rename(global_apps, None)

    assert _names(renamed) == ["Ideias soltas", "Em avaliação", "Aprovadas", "Descartadas"]
    assert _names(shuffled) == ["A fazer", "Sem responsável", "Pronto", "Em andamento"]


@pytest.mark.django_db
def test_finished_ideas_are_not_filed_as_discarded():
    board = _board()
    Idea.objects.create(workspace=board[3].workspace, group=board[3], title="Já publicada")

    migration.rename(global_apps, None)

    # "Done" changing into "Discarded" would misfile what is in it.
    assert _names(board) == ["Brainstorming", "Em avaliação", "Aprovadas", "Pronto"]


@pytest.mark.django_db
def test_the_rename_can_be_taken_back():
    board = _board()
    migration.rename(global_apps, None)
    migration.restore(global_apps, None)
    assert _names(board) == list(ORIGINAL)
