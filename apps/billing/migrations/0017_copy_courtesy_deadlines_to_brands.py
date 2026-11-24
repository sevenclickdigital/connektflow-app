from django.db import migrations


def copy_account_deadlines(apps, schema_editor):
    """Give each exempt brand the end date its account's exemption had.

    The deadline lived on the account; it now lives on the brand, which is
    what the expiry reads. Brands that already have a date are left alone.
    """
    Profile = apps.get_model("billing", "OrganizationBillingProfile")
    Subscription = apps.get_model("billing", "WorkspaceSubscription")
    for profile in Profile.objects.filter(courtesy_expires_on__isnull=False):
        Subscription.objects.filter(
            workspace__organization_id=profile.organization_id,
            status="courtesy",
            courtesy_expires_on__isnull=True,
        ).update(courtesy_expires_on=profile.courtesy_expires_on)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0016_brand_courtesy_deadline"),
    ]

    operations = [
        migrations.RunPython(copy_account_deadlines, reverse_code=migrations.RunPython.noop),
    ]
