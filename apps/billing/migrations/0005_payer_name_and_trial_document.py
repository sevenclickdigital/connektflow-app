from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0004_billing_cycle_and_trial"),
    ]

    operations = [
        # ADD COLUMN statements with a constant default kept in the database: they
        # only touch the catalogue, and the release still running keeps
        # inserting rows without them. The brief exclusive lock they need
        # fails fast instead of queueing every request behind a long
        # transaction.
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="organizationbillingprofile",
            name="payer_name",
            field=models.CharField(blank=True, db_default="", default="", max_length=255),
        ),
        migrations.AddField(
            model_name="organizationbillingprofile",
            name="trial_tax_id",
            field=models.CharField(blank=True, db_default="", default="", max_length=14),
        ),
        migrations.AddField(
            model_name="organizationbillingprofile",
            name="trial_phone",
            field=models.CharField(blank=True, db_default="", default="", max_length=20),
        ),
        migrations.AddField(
            model_name="organizationbillingprofile",
            name="trial_email",
            field=models.CharField(blank=True, db_default="", default="", max_length=254),
        ),
    ]
