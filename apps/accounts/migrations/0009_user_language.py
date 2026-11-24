"""The language each person receives e-mails in.

Adding a column with a constant database default is a catalogue change in
PostgreSQL 16 (no table rewrite), so it holds the users table's lock for an
instant; lock_timeout makes the deploy fail fast instead of queueing behind a
long transaction. Existing accounts get Portuguese, the product's default,
through the column's default; old code inserting during the deploy gets it too.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0008_verify_existing_email_addresses"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="user",
            name="language",
            field=models.CharField(db_default="pt-br", default="pt-br", max_length=10),
        ),
    ]
