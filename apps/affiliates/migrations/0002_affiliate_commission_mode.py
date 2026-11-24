from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("affiliates", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="affiliate",
            name="commission_mode",
            field=models.CharField(
                choices=[("recurring", "Monthly, on every payment"), ("once", "Once per account")],
                db_default="recurring",
                default="recurring",
                max_length=10,
            ),
        ),
    ]
