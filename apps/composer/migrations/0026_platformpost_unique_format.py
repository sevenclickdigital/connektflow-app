from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("composer", "0025_platformpost_format"),
    ]

    operations = [
        # Added before the old constraint is dropped, so (post, account) stays
        # unique at every step; it can't fail on existing rows for that reason.
        migrations.AddConstraint(
            model_name="platformpost",
            constraint=models.UniqueConstraint(
                fields=("post", "social_account", "format"),
                name="uniq_pp_post_account_format",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="platformpost",
            unique_together=set(),
        ),
    ]
