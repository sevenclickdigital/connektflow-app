"""Give every template that shipped with the product its production sheet: the briefing
for the creative team and the arts to produce (IdeaTemplate.briefing / art_items), sent on
to the task of "Send as task".

The sheets are in data/template_arts.json, keyed by the template's id (the shipped
templates keep the ids of builtin_templates.py in every database). A template whose
briefing or arts the operator already filled is left as it is.
"""

import json
from pathlib import Path

from django.db import migrations

SHEETS = Path(__file__).parent / "data" / "template_arts.json"


def fill(apps, schema_editor):
    IdeaTemplate = apps.get_model("composer", "IdeaTemplate")
    sheets = json.loads(SHEETS.read_text(encoding="utf-8"))
    for template_id, sheet in sheets.items():
        IdeaTemplate.objects.filter(id=int(template_id), briefing="", art_items=[]).update(
            briefing=sheet["briefing"], art_items=sheet["art_items"]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("composer", "0040_template_and_idea_arts"),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
