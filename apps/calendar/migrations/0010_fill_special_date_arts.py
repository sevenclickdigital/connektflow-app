"""Give the special dates of the starter segments (0008_seed_date_segments) their
production sheet: the briefing for the creative team and the arts to produce
(SpecialDate.briefing / art_items), sent on to the task of "Send as task".

The sheets are in data/special_date_arts.json. Those dates were created with new ids in
each database, so a date is found by its segment's name, its name and its day. A date
whose briefing or arts the operator already filled is left as it is.
"""

import json
from pathlib import Path

from django.db import migrations

SHEETS = Path(__file__).parent / "data" / "special_date_arts.json"


def fill(apps, schema_editor):
    SpecialDate = apps.get_model("calendar", "SpecialDate")
    for sheet in json.loads(SHEETS.read_text(encoding="utf-8")):
        SpecialDate.objects.filter(
            segment__name=sheet["segment"],
            name=sheet["name"],
            month=sheet["month"],
            day=sheet["day"],
            briefing="",
            art_items=[],
        ).update(briefing=sheet["briefing"], art_items=sheet["art_items"])


class Migration(migrations.Migration):
    dependencies = [
        ("calendar", "0009_special_date_arts"),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
