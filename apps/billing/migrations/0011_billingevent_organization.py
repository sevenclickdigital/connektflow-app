import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0010_plan_brand_addition"),
        ("organizations", "0007_role_permissions"),
    ]

    operations = [
        # ADD COLUMN (nullable, no default) is metadata-only, and the foreign
        # key is added NOT VALID so no row is scanned under the lock; both
        # still take a short lock on billing_event and on
        # organizations_organization: fail fast instead of queueing.
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="billingevent",
                    name="organization",
                    field=models.ForeignKey(
                        blank=True,
                        db_index=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="billing_events",
                        to="organizations.organization",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                        ALTER TABLE billing_event ADD COLUMN organization_id uuid NULL;
                        ALTER TABLE billing_event
                            ADD CONSTRAINT billing_event_organization_id_fk
                            FOREIGN KEY (organization_id) REFERENCES organizations_organization (id)
                            DEFERRABLE INITIALLY DEFERRED NOT VALID;
                    """,
                    reverse_sql="ALTER TABLE billing_event DROP COLUMN organization_id;",
                ),
            ],
        ),
    ]
