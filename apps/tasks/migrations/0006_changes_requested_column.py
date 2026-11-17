from django.conf import settings
from django.db import migrations, models
from django.utils.translation import gettext, override


def add_changes_requested(apps, schema_editor):
    """Boards that already exist get "Changes requested" (red), right before their done
    column (at the end when they have none), named in the site's language as the
    default columns are. A board that already has a column of that name gets it flagged
    instead of a second one."""
    TaskColumn = apps.get_model("tasks", "TaskColumn")
    with override(settings.LANGUAGE_CODE):
        name = gettext("Changes requested")
    workspace_ids = TaskColumn.objects.values_list("workspace_id", flat=True).distinct()
    for workspace_id in workspace_ids:
        columns = list(TaskColumn.objects.filter(workspace_id=workspace_id).order_by("position", "created_at"))
        named = [column for column in columns if column.name.strip().lower() == name.lower()]
        if named:
            TaskColumn.objects.filter(pk=named[0].pk).update(is_changes_requested=True)
            continue
        done = next((column for column in columns if column.is_done), None)
        at = columns.index(done) if done else len(columns)
        for position, column in enumerate(columns[:at] + [None] + columns[at:]):
            if column is None:
                TaskColumn.objects.create(
                    workspace_id=workspace_id, name=name, tone="error", position=position, is_changes_requested=True
                )
            elif column.position != position:
                column.position = position
                column.save(update_fields=["position"])


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0005_task_briefing_channels"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="taskcolumn",
            name="is_changes_requested",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.RunPython(add_changes_requested, migrations.RunPython.noop),
    ]
