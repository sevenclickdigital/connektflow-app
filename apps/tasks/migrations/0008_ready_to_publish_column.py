from django.conf import settings
from django.db import migrations, models
from django.utils.translation import gettext, override


def rename_done_columns(apps, schema_editor):
    """The done columns still named as the default one was ("Done", in the site's language)
    become "Ready to publish"; a done column the team renamed keeps its name."""
    TaskColumn = apps.get_model("tasks", "TaskColumn")
    with override(settings.LANGUAGE_CODE):
        old, new = gettext("Done"), gettext("Ready to publish")
    for column in TaskColumn.objects.filter(is_done=True):
        if column.name.strip().lower() == old.lower():
            column.name = new
            column.save(update_fields=["name"])


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0007_checklist_comments"),
    ]

    operations = [
        migrations.RunPython(rename_done_columns, migrations.RunPython.noop),
        # The review column's help text only (no SQL): tasks go there from their window's button.
        migrations.AlterField(
            model_name="taskcolumn",
            name="is_review",
            field=models.BooleanField(
                default=False, help_text="Where a task goes when someone sends it for review from its window."
            ),
        ),
    ]
