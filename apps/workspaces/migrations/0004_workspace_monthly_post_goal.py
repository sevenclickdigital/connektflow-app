from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0003_alter_workspace_primary_color_and_more"),
    ]

    operations = [
        # A nullable column is a catalogue change, but ADD COLUMN still takes
        # ACCESS EXCLUSIVE on a table every request reads: fail fast instead of
        # queueing behind a long transaction.
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="workspace",
            name="monthly_post_goal",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
