import django.db.models.deletion
from django.db import migrations, models

FK_NAME = "media_library_media__original_id_f32bdb51_fk_media_lib"
INDEX_NAME = "media_library_media_asset_original_id_f32bdb51"


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY and VALIDATE CONSTRAINT cannot run inside a
    # transaction, so each statement below runs on its own.
    atomic = False

    dependencies = [
        ("media_library", "0006_asset_author"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="mediaasset",
                    name="original",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="versions_of",
                        to="media_library.mediaasset",
                    ),
                ),
            ],
            database_operations=[
                # The column is new and empty: NOT VALID skips the scan, and the
                # timeout makes the brief lock fail fast instead of queueing.
                migrations.RunSQL(
                    sql=f"""
                        SET LOCAL lock_timeout = '2s';
                        ALTER TABLE media_library_media_asset ADD COLUMN original_id uuid NULL;
                        ALTER TABLE media_library_media_asset
                            ADD CONSTRAINT {FK_NAME}
                            FOREIGN KEY (original_id) REFERENCES media_library_media_asset(id)
                            DEFERRABLE INITIALLY DEFERRED NOT VALID;
                    """,
                    reverse_sql=f"""
                        SET LOCAL lock_timeout = '2s';
                        ALTER TABLE media_library_media_asset DROP CONSTRAINT IF EXISTS {FK_NAME};
                        ALTER TABLE media_library_media_asset DROP COLUMN IF EXISTS original_id;
                    """,
                ),
                migrations.RunSQL(
                    sql=f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} ON media_library_media_asset (original_id);",
                    reverse_sql=f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME};",
                ),
                # Every row is NULL, so this only confirms it, without blocking writes.
                migrations.RunSQL(
                    sql=f"ALTER TABLE media_library_media_asset VALIDATE CONSTRAINT {FK_NAME};",
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
        ),
    ]
