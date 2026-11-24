"""The texts saved before e-mails had a language become the Portuguese ones."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

APP = "email_center"
BEFORE = "0001_initial"
AFTER = "0002_system_email_languages"


def _migrate(target: str):
    executor = MigrationExecutor(connection)
    executor.migrate([(APP, target)])
    return MigrationExecutor(connection).loader.project_state((APP, target)).apps


@pytest.fixture
def back_to_head():
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes(APP))


@pytest.mark.django_db(transaction=True)
def test_existing_versions_become_portuguese_and_edited(back_to_head):
    old_apps = _migrate(BEFORE)
    old_apps.get_model(APP, "SystemEmailOverride").objects.create(
        key="members/email/invite", subject="Convite", body_markdown="# Oi", is_active=True
    )

    new_apps = _migrate(AFTER)
    version = new_apps.get_model(APP, "SystemEmailOverride")

    row = version.objects.get(key="members/email/invite")
    assert row.language == "pt-br" and not row.is_default_text and row.is_active
    # The same e-mail can now have its English version beside it.
    version.objects.create(key="members/email/invite", language="en-us", subject="Invitation", body_markdown="# Hi")
    assert version.objects.filter(key="members/email/invite").count() == 2


@pytest.mark.django_db(transaction=True)
def test_existing_automatic_emails_keep_their_text_in_portuguese(back_to_head):
    old_apps = _migrate(AFTER)
    old_apps.get_model(APP, "AutomatedEmail").objects.create(
        name="Boas-vindas", subject="Oi {{ name }}", body_markdown="# Oi", delay_days=3
    )

    new_apps = _migrate("0003_automated_email_texts")
    email = new_apps.get_model(APP, "AutomatedEmail")
    text = new_apps.get_model(APP, "AutomatedEmailText").objects.get()

    assert (text.language, text.subject, text.body_markdown) == ("pt-br", "Oi {{ name }}", "# Oi")
    # The new code, which no longer writes the old columns, can still add e-mails.
    email.objects.create(name="Outro", delay_days=1)
    assert email.objects.count() == 2
