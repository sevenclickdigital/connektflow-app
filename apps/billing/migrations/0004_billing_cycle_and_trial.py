from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0003_productprice_productpricechange"),
    ]

    operations = [
        # Every ADD COLUMN here is nullable or has a constant default kept in
        # the database, so each only touches the catalogue and the release
        # still running can keep inserting rows without the new columns. They
        # still need a brief exclusive lock on each table: fail fast instead of
        # queueing every request behind a long transaction.
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="workspacesubscription",
            name="billing_cycle",
            field=models.CharField(
                choices=[("monthly", "Monthly"), ("yearly", "Yearly")],
                db_default="monthly",
                default="monthly",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="workspacesubscription",
            name="trial_ends_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="organizationbillingprofile",
            name="tax_id",
            field=models.CharField(blank=True, db_default="", default="", max_length=14),
        ),
        migrations.AddField(
            model_name="organizationbillingprofile",
            name="phone",
            field=models.CharField(blank=True, db_default="", default="", max_length=20),
        ),
        migrations.AddField(
            model_name="organizationbillingprofile",
            name="trial_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
