"""One version of each system e-mail per language.

The table holds a handful of rows (the team's own texts), so dropping the
unique key and adding the (key, language) one take their locks for an instant;
lock_timeout makes the deploy fail fast instead of queueing behind a long
transaction. Existing rows were written in Portuguese and keep that language
through the column's database default; old code inserting during the deploy
gets the same defaults.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("email_center", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="systememailoverride",
            name="is_default_text",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name="systememailoverride",
            name="language",
            field=models.CharField(db_default="pt-br", default="pt-br", max_length=10),
        ),
        migrations.AlterField(
            model_name="systememailoverride",
            name="key",
            field=models.CharField(max_length=120),
        ),
        migrations.AddConstraint(
            model_name="systememailoverride",
            constraint=models.UniqueConstraint(
                fields=("key", "language"), name="email_center_one_version_per_language"
            ),
        ),
    ]
