"""A page's tutorial gets a text beside its video, and may have the text alone.

``tutorial_markdown`` has a database default, so the release still running
during the deploy, which does not send it, keeps inserting. ``video_url``
only becomes optional in the form (no SQL).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("onboarding", "0003_alter_pagehelpvideo_page"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="pagehelpvideo",
            name="tutorial_markdown",
            field=models.TextField(blank=True, db_default="", default=""),
        ),
        migrations.AlterField(
            model_name="pagehelpvideo",
            name="video_url",
            field=models.URLField(blank=True, max_length=300),
        ),
    ]
