import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0003_task_tags"),
    ]

    # Additive only: a new table, and a nullable column on the checklist items
    # (old code never reads it). The items table is small (to-dos of tasks), so
    # the foreign key's index builds in a moment; lock_timeout makes the step
    # fail fast instead of queueing behind a long transaction.
    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.CreateModel(
            name="TaskChecklist",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=120)),
                ("position", models.PositiveIntegerField(default=0)),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="checklists", to="tasks.task"
                    ),
                ),
            ],
            options={
                "db_table": "tasks_checklist",
                "ordering": ["position", "id"],
            },
        ),
        migrations.AddField(
            model_name="taskchecklistitem",
            name="checklist",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="items",
                to="tasks.taskchecklist",
            ),
        ),
    ]
