"""Migration 0041 over the shipped templates: each gets its production sheet, except one
whose briefing or arts the operator already filled."""

import importlib

import pytest
from django.apps import apps

from apps.composer.models import IdeaTemplate
from apps.tasks.creative_formats import clean_art_items

migration = importlib.import_module("apps.composer.migrations.0041_fill_template_arts")


@pytest.mark.django_db
def test_every_shipped_template_gets_a_sheet_and_the_operators_own_is_kept():
    IdeaTemplate.objects.filter(id=161).update(briefing="", art_items=[])
    IdeaTemplate.objects.filter(id=1).update(briefing="Mine", art_items=[])

    migration.fill(apps, None)

    step_by_step = IdeaTemplate.objects.get(id=161)
    assert step_by_step.briefing and step_by_step.art_items
    assert clean_art_items(step_by_step.art_items) == step_by_step.art_items
    assert IdeaTemplate.objects.get(id=1).art_items == [] and IdeaTemplate.objects.get(id=1).briefing == "Mine"


def test_the_sheets_cover_the_shipped_templates_with_valid_arts():
    import json

    from apps.composer.builtin_templates import TEMPLATES

    sheets = json.loads(migration.SHEETS.read_text(encoding="utf-8"))
    assert set(sheets) == {str(t["id"]) for t in TEMPLATES}
    for sheet in sheets.values():
        assert sheet["briefing"] and clean_art_items(sheet["art_items"]) == sheet["art_items"]
