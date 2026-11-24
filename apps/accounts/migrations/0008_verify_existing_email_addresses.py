from django.db import IntegrityError, migrations, transaction


def verify_existing_addresses(apps, schema_editor):
    """Count the address of every account that predates e-mail verification as verified.

    Verification becomes mandatory with this release; these accounts were
    created when nothing asked for it and must keep signing in. Each account
    gets a verified row for its own address (the existing one, or a new one).
    An address that only differs in case from another account's verified one
    cannot be marked (allauth allows one verified row per address): that
    account is left to confirm by code the next time it signs in.

    Idempotent, and safe to run late: an address verified since is left as it is.
    """
    User = apps.get_model("accounts", "User")
    EmailAddress = apps.get_model("account", "EmailAddress")

    for user in User.objects.exclude(email="").only("id", "email").iterator():
        try:
            with transaction.atomic():
                rows = EmailAddress.objects.filter(user_id=user.id)
                own = rows.filter(email__iexact=user.email).first()
                if own is None:
                    EmailAddress.objects.create(
                        user_id=user.id,
                        email=user.email.lower(),
                        verified=True,
                        primary=not rows.filter(primary=True).exists(),
                    )
                elif not own.verified:
                    own.verified = True
                    own.save(update_fields=["verified"])
        except IntegrityError:
            continue


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_user_profile_and_address"),
        ("account", "0009_emailaddress_unique_primary_email"),
    ]

    operations = [
        migrations.RunPython(verify_existing_addresses, reverse_code=migrations.RunPython.noop),
    ]
