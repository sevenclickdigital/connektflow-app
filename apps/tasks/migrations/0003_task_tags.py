from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0002_column_tone"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="task",
            name="tags",
            field=models.JSONField(
                blank=True, db_default=models.Value([], output_field=models.JSONField()), default=list
            ),
        ),
    ]
