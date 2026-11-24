from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("affiliates", "0002_affiliate_commission_mode"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="payout",
            name="receipt",
            field=models.FileField(blank=True, db_default="", default="", upload_to="affiliate_payouts/%Y/%m/"),
        ),
    ]
