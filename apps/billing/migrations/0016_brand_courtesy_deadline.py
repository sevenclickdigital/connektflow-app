from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0015_organizationplan_removal_warned_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspacesubscription",
            name="courtesy_expires_on",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workspacesubscription",
            name="courtesy_granted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
