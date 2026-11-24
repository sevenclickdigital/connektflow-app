from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # VALIDATE CONSTRAINT and CREATE INDEX CONCURRENTLY block neither reads nor
    # writes, and neither may run inside the migration's transaction.
    atomic = False

    dependencies = [
        ("billing", "0011_billingevent_organization"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE billing_event VALIDATE CONSTRAINT billing_event_organization_id_fk;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        AddIndexConcurrently(
            model_name="billingevent",
            index=models.Index(fields=["organization", "-created_at"], name="billing_event_org_created"),
        ),
    ]
