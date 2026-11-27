"""When a failed publication was marked as resolved.

A nullable column without a default is a catalogue change in PostgreSQL 16 (no
table rewrite, no scan), so it holds the table's lock for an instant;
lock_timeout makes the deploy fail fast instead of queueing behind a long
transaction. Old code never reads or writes the column.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("composer", "0042_platformpost_format_short"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="platformpost",
            name="failure_dismissed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
