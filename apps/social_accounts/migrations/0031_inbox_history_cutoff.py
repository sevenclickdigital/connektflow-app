from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("social_accounts", "0030_granted_by_platform_user_id"),
    ]

    operations = [
        # A nullable column is a catalogue change, but still takes a brief
        # exclusive lock on the accounts table. Fail fast instead of queueing
        # every request behind a long transaction.
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="socialaccount",
            name="inbox_history_cutoff",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
