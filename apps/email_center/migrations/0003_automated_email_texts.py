"""Automatic e-mails get one subject and text per language.

The texts move to ``email_center_automated_email_text``; each existing
e-mail's subject and text become its Portuguese text, the language they were
written in.

The old ``subject`` and ``body_markdown`` columns leave the model but stay in
the table for now: the release still running during the deploy reads and
writes them. They get an empty database default so the new code, which no
longer sends them, can insert. A later release drops them.
"""

import django.db.models.deletion
from django.db import migrations, models


def copy_texts(apps, schema_editor):
    AutomatedEmail = apps.get_model("email_center", "AutomatedEmail")
    AutomatedEmailText = apps.get_model("email_center", "AutomatedEmailText")
    AutomatedEmailText.objects.bulk_create(
        [
            AutomatedEmailText(
                automated_email_id=email.pk, language="pt-br", subject=email.subject, body_markdown=email.body_markdown
            )
            for email in AutomatedEmail.objects.all()
        ],
        ignore_conflicts=True,
    )


def copy_texts_back(apps, schema_editor):
    AutomatedEmail = apps.get_model("email_center", "AutomatedEmail")
    AutomatedEmailText = apps.get_model("email_center", "AutomatedEmailText")
    for text in AutomatedEmailText.objects.filter(language="pt-br"):
        AutomatedEmail.objects.filter(pk=text.automated_email_id).update(
            subject=text.subject, body_markdown=text.body_markdown
        )


class Migration(migrations.Migration):
    dependencies = [
        ("email_center", "0002_system_email_languages"),
    ]

    operations = [
        migrations.CreateModel(
            name="AutomatedEmailText",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("language", models.CharField(max_length=10)),
                ("subject", models.CharField(max_length=200)),
                ("body_markdown", models.TextField()),
                (
                    "automated_email",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="texts",
                        to="email_center.automatedemail",
                    ),
                ),
            ],
            options={
                "db_table": "email_center_automated_email_text",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("automated_email", "language"), name="email_center_one_text_per_language"
                    )
                ],
            },
        ),
        migrations.RunPython(copy_texts, copy_texts_back),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="automatedemail", name="body_markdown"),
                migrations.RemoveField(model_name="automatedemail", name="subject"),
            ],
            database_operations=[
                migrations.RunSQL(
                    "SET LOCAL lock_timeout = '2s';"
                    "ALTER TABLE email_center_automated_email ALTER COLUMN subject SET DEFAULT '';"
                    "ALTER TABLE email_center_automated_email ALTER COLUMN body_markdown SET DEFAULT '';",
                    reverse_sql=(
                        "ALTER TABLE email_center_automated_email ALTER COLUMN subject DROP DEFAULT;"
                        "ALTER TABLE email_center_automated_email ALTER COLUMN body_markdown DROP DEFAULT;"
                    ),
                ),
            ],
        ),
    ]
