from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("social_accounts", "0029_data_deletion_request"),
    ]

    operations = [
        # ADD COLUMN with a constant default only touches the catalogue, but it
        # still needs a brief exclusive lock on the accounts table. Fail fast
        # instead of queueing every request behind a long transaction.
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="socialaccount",
            name="granted_by_platform_user_id",
            field=models.CharField(blank=True, db_default="", default="", max_length=255),
        ),
    ]
