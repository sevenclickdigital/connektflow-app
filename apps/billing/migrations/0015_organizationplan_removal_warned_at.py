from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0014_cancels_on"),
    ]

    operations = [
        # ADD COLUMN (nullable, no default) is metadata-only, but still takes
        # ACCESS EXCLUSIVE on the table: fail fast instead of queueing.
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="organizationplan",
            name="removal_warned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
