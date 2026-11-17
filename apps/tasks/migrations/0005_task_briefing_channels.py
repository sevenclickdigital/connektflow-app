from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0004_named_checklists"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="task",
            name="briefing",
            field=models.TextField(blank=True, db_default="", default=""),
        ),
        migrations.AddField(
            model_name="task",
            name="channel_formats",
            field=models.JSONField(
                blank=True, db_default=models.Value({}, output_field=models.JSONField()), default=dict
            ),
        ),
    ]
