from django.db import migrations, models


def colour_existing_columns(apps, schema_editor):
    """Columns that already exist: the done ones green, the review ones yellow, the rest keep purple."""
    TaskColumn = apps.get_model("tasks", "TaskColumn")
    TaskColumn.objects.filter(is_done=True).update(tone="success")
    TaskColumn.objects.filter(is_review=True, is_done=False).update(tone="warning")


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="taskcolumn",
            name="tone",
            field=models.CharField(
                choices=[
                    ("brand", "Purple"),
                    ("info", "Blue"),
                    ("warning", "Yellow"),
                    ("success", "Green"),
                    ("error", "Red"),
                ],
                db_default="brand",
                default="brand",
                max_length=10,
            ),
        ),
        migrations.RunPython(colour_existing_columns, migrations.RunPython.noop),
    ]
