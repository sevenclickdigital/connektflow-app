from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0013_backfill_billingevent_organization"),
    ]

    operations = [
        # Each ADD COLUMN (nullable, no default) is metadata-only, but still
        # takes ACCESS EXCLUSIVE on its table: fail fast instead of queueing.
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="organizationplan",
            name="cancels_on",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workspacesubscription",
            name="cancels_on",
            field=models.DateField(blank=True, null=True),
        ),
    ]
