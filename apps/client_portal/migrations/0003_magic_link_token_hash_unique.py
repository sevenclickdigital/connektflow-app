"""Unique index on ``token_hash``, built without blocking writes.

The index is built concurrently, then attached as the constraint the model
declares; only that last step takes a brief ACCESS EXCLUSIVE lock.
"""

from django.db import migrations, models

TABLE = "client_portal_magic_link_token"
NAME = "uniq_magic_token_hash"


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("client_portal", "0002_magic_link_token_hash"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name="magiclinktoken",
                    constraint=models.UniqueConstraint(fields=("token_hash",), name=NAME),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {NAME} ON {TABLE} (token_hash);",
                    reverse_sql=f"DROP INDEX CONCURRENTLY IF EXISTS {NAME};",
                ),
                migrations.RunSQL(
                    f"SET LOCAL lock_timeout = '2s'; ALTER TABLE {TABLE} ADD CONSTRAINT {NAME} UNIQUE USING INDEX {NAME};",
                    reverse_sql=f"SET LOCAL lock_timeout = '2s'; ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {NAME};",
                ),
            ],
        ),
    ]
