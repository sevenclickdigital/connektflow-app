import json
from pathlib import Path

from django.db import migrations

#: The shelf in Brazilian Portuguese, with its details, characteristics and
#: two more versions of each text (``variations``), by template id.
DATA = Path(__file__).resolve().parent.parent / "idea_templates_pt_br.json"


def translate(apps, schema_editor):
    """Bring the templates that shipped in English to Portuguese and fill in
    what the cards show now.

    A field is replaced only while it still holds the text it shipped with,
    and the new fields only while empty: what the operator already rewrote
    stays. Versions are added only to a template that has none.
    """
    from apps.composer.builtin_templates import TEMPLATES

    IdeaTemplate = apps.get_model("composer", "IdeaTemplate")
    IdeaTemplateVariation = apps.get_model("composer", "IdeaTemplateVariation")
    shipped = {tpl["id"]: tpl for tpl in TEMPLATES}

    for item in json.loads(DATA.read_text(encoding="utf-8")):
        template = IdeaTemplate.objects.filter(id=item["id"]).first()
        if template is None:
            continue
        original = shipped.get(template.id, {})
        changed = []
        for field in ("title", "description", "body"):
            # A template that shipped without a text (several had none) counts as untouched while empty.
            if original and getattr(template, field) == original.get(field, ""):
                setattr(template, field, item[field])
                changed.append(field)
        for field in ("details", "ideal_for", "formats", "goals", "networks"):
            if not getattr(template, field):
                setattr(template, field, item[field])
                changed.append(field)
        if changed:
            template.save(update_fields=changed)
        if not IdeaTemplateVariation.objects.filter(template=template).exists():
            IdeaTemplateVariation.objects.bulk_create(
                IdeaTemplateVariation(template=template, title=version["title"], body=version["body"], position=index)
                for index, version in enumerate(item["variations"])
            )


class Migration(migrations.Migration):
    dependencies = [
        ("composer", "0033_idea_template_details"),
    ]

    operations = [
        migrations.RunPython(translate, reverse_code=migrations.RunPython.noop),
    ]
