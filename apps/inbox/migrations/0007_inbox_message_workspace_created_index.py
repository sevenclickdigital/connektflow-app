from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction, and builds the
    # index without blocking writes to the inbox table.
    atomic = False

    dependencies = [
        ("inbox", "0006_inboxmessage_api_refreshed_at"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="inboxmessage",
            index=models.Index(fields=["workspace", "-created_at"], name="inbox_msg_ws_created"),
        ),
    ]
