from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0008_inbox_message_tag_type"),
    ]

    operations = [
        # Adding the column is a catalogue change, but it needs the table's
        # exclusive lock for an instant: give up after two seconds instead of
        # queueing every inbox query behind a long transaction.
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="inboxreply",
            name="extra",
            field=models.JSONField(blank=True, db_default=models.Value({}, models.JSONField()), default=dict),
        ),
    ]
