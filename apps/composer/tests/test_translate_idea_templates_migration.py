"""Migration 0034 over rows that already exist: the shipped English text
becomes Portuguese, what the operator rewrote stays, and versions are added
only where there are none."""

import importlib
import json

import pytest
from django.apps import apps

from apps.composer.builtin_templates import TEMPLATES
from apps.composer.models import IdeaTemplate, IdeaTemplateVariation

migration = importlib.import_module("apps.composer.migrations.0034_translate_idea_templates")
DATA = {item["id"]: item for item in json.loads(migration.DATA.read_text(encoding="utf-8"))}
SHIPPED = {tpl["id"]: tpl for tpl in TEMPLATES}


def test_the_data_covers_every_shipped_template_with_valid_choices():
    assert set(DATA) == set(SHIPPED)
    for item in DATA.values():
        assert item["title"] and item["body"] and item["details"] and item["ideal_for"]
        assert set(item["formats"]) <= set(IdeaTemplate.Format.values) and item["formats"]
        assert set(item["goals"]) <= set(IdeaTemplate.Goal.values) and item["goals"]
        assert set(item["networks"]) <= set(IdeaTemplate.Network.values) and item["networks"]
        assert len(item["variations"]) == 2 and all(version["body"] for version in item["variations"])


@pytest.mark.django_db
def test_the_migration_translates_untouched_rows_and_keeps_the_operators_edits():
    untouched_id, edited_id = sorted(DATA)[:2]
    shipped = SHIPPED[untouched_id]
    IdeaTemplate.objects.filter(id=untouched_id).update(
        title=shipped["title"],
        description=shipped.get("description", ""),
        body=shipped.get("body", ""),
        details="",
        formats=[],
    )
    IdeaTemplateVariation.objects.filter(template_id=untouched_id).delete()
    IdeaTemplate.objects.filter(id=edited_id).update(title="Título do operador", details="Do operador")
    kept = IdeaTemplateVariation.objects.filter(template_id=edited_id).count()

    migration.translate(apps, None)

    untouched = IdeaTemplate.objects.get(id=untouched_id)
    assert (untouched.title, untouched.body) == (DATA[untouched_id]["title"], DATA[untouched_id]["body"])
    assert untouched.formats == DATA[untouched_id]["formats"]
    assert untouched.variations.count() == 2
    edited = IdeaTemplate.objects.get(id=edited_id)
    assert (edited.title, edited.details) == ("Título do operador", "Do operador")
    # Running it again adds nothing.
    assert IdeaTemplateVariation.objects.filter(template_id=edited_id).count() == kept
