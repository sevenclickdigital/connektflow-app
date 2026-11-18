"""Account mail: branded HTML on allauth's messages, the welcome email, the password notice."""

import pytest
from django.core import mail
from django.urls import reverse


def _html(message):
    return next(body for body, mimetype in message.alternatives if mimetype == "text/html")


@pytest.mark.django_db
def test_the_password_reset_email_carries_the_branded_layout(client, user):
    client.post(reverse("account_reset_password"), {"email": user.email})

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    html = _html(message)
    assert "Connekt Flow" in message.subject
    assert "img/email/connektflow-wordmark-dark.png" in html
    assert "/accounts/password/reset/key/" in html
    assert "/privacidade/" in html and "/termos/" in html
    # The text part keeps the reset link and the footer, without the upstream name.
    assert "/accounts/password/reset/key/" in message.body
    assert "brightbean" not in (message.subject + message.body + html).lower()


@pytest.mark.django_db
def test_signing_up_sends_a_welcome_email(client, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("account_signup"), {"email": "nova@example.com", "password1": "Uma-senha-forte-42"})

    welcome = [m for m in mail.outbox if m.to == ["nova@example.com"]]
    assert len(welcome) == 1
    assert "Connekt Flow" in welcome[0].subject
    assert "img/email/connektflow-wordmark-dark.png" in _html(welcome[0])


@pytest.mark.django_db
def test_changing_the_password_in_settings_emails_a_security_notice(client, user):
    client.force_login(user)

    client.post(
        reverse("accounts:settings"),
        {
            "action": "update_password",
            "current_password": "testpass123",
            "password": "Outra-senha-forte-42",
            "password_confirm": "Outra-senha-forte-42",
        },
    )

    assert [m.to for m in mail.outbox] == [[user.email]]
    html = _html(mail.outbox[0])
    assert reverse("account_reset_password") in html  # the "not me" way out
