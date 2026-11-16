from django.db import migrations


def seed(apps, schema_editor):
    """Fill the shelf with the templates that shipped in the code, keeping
    their ids: links of the form ``?template=<id>`` keep working.

    ``builtin_templates.TEMPLATES`` is only this seed from here on; the table
    is what the product reads, and the operator edits it. Rows that already
    exist are left as they are.
    """
    from apps.composer.builtin_templates import TEMPLATES

    IdeaTemplate = apps.get_model("composer", "IdeaTemplate")
    existing = set(IdeaTemplate.objects.values_list("id", flat=True))
    IdeaTemplate.objects.bulk_create(
        IdeaTemplate(
            id=tpl["id"],
            emoji=tpl.get("emoji", ""),
            title=tpl["title"],
            description=tpl.get("description", ""),
            body=tpl.get("body", ""),
            category=tpl["category"],
        )
        for tpl in TEMPLATES
        if tpl["id"] not in existing
    )
    # The ids were given by hand, so the sequence has to be moved past them.
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "SELECT setval(pg_get_serial_sequence('composer_idea_template', 'id'), "
                "COALESCE((SELECT MAX(id) FROM composer_idea_template), 1))"
            )


class Migration(migrations.Migration):
    dependencies = [
        ("composer", "0028_idea_templates"),
    ]

    operations = [
        migrations.RunPython(seed, reverse_code=migrations.RunPython.noop),
    ]
