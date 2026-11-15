from django.db import migrations

BRASILIA = "America/Sao_Paulo"


def utc_to_brasilia(apps, schema_editor):
    """Move organizations still on the old sign-up default to Brasília time.

    "UTC" was what every organization was created with, not a choice anyone
    made, and the screen where it could be changed is gone: the timezone is
    now set per brand. An organization on any other timezone chose it (or
    its browser did at sign-up) and is left alone, as is every brand with a
    timezone of its own.
    """
    Organization = apps.get_model("organizations", "Organization")
    Organization.objects.filter(default_timezone="UTC").update(default_timezone=BRASILIA)


class Migration(migrations.Migration):
    dependencies = [
        ("organizations", "0002_organization_billing_email"),
    ]

    operations = [
        # Not reversed: afterwards nothing tells an organization that was on
        # UTC from one that chose Brasília.
        migrations.RunPython(utc_to_brasilia, migrations.RunPython.noop),
    ]
