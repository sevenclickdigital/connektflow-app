"""Store magic-link tokens as a peppered hash instead of plaintext.

Rolling deploy: the previous release keeps serving while this runs, reading and
writing the plaintext ``token`` column. That column stays (now nullable, since
new rows leave it empty) and is dropped in a later release. Existing rows are
hashed and their plaintext cleared here, so links already e-mailed keep working
under the new code.
"""

from django.db import migrations, models

from apps.client_portal.models import _generate_magic_token, hash_magic_token


def hash_existing_tokens(apps, schema_editor):
    MagicLinkToken = apps.get_model("client_portal", "MagicLinkToken")
    rows = MagicLinkToken.objects.filter(token__isnull=False, token_hash__isnull=True).only("pk", "token")
    for row in rows.iterator():
        MagicLinkToken.objects.filter(pk=row.pk).update(token_hash=hash_magic_token(row.token), token=None)


def refill_plaintext_column(apps, schema_editor):
    """Give hashed rows a throwaway token so ``token`` can be NOT NULL again.

    The real tokens cannot be recovered from their hashes, so links sent under
    the hashed scheme stop working after a rollback.
    """
    MagicLinkToken = apps.get_model("client_portal", "MagicLinkToken")
    for pk in MagicLinkToken.objects.filter(token__isnull=True).values_list("pk", flat=True).iterator():
        MagicLinkToken.objects.filter(pk=pk).update(token=_generate_magic_token())


class Migration(migrations.Migration):
    dependencies = [
        ("client_portal", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL lock_timeout = '2s'", reverse_sql=migrations.RunSQL.noop),
        migrations.AddField(
            model_name="magiclinktoken",
            name="token_hash",
            field=models.CharField(blank=True, editable=False, max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name="magiclinktoken",
            name="token",
            field=models.CharField(blank=True, db_index=True, editable=False, max_length=128, null=True, unique=True),
        ),
        migrations.RunPython(hash_existing_tokens, reverse_code=refill_plaintext_column),
        # Runs first when reversing, ahead of SET NOT NULL on ``token``.
        migrations.RunSQL(migrations.RunSQL.noop, reverse_sql="SET LOCAL lock_timeout = '2s'"),
    ]
