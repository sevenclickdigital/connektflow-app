"""Account mail: branded HTML on allauth's messages, the welcome email, the password notice."""

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.models import User


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
def test_signing_up_sends_a_welcome_email_that_greets_by_first_name(client, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        client.post(
            reverse("account_signup"),
            {"full_name": "  Ana   Souza ", "email": "nova@example.com", "password1": "Uma-senha-forte-42"},
        )

    user = User.objects.get(email="nova@example.com")
    assert (user.first_name, user.last_name, user.name) == ("Ana", "Souza", "Ana Souza")
    welcome = [m for m in mail.outbox if m.to == ["nova@example.com"]]
    assert len(welcome) == 1
    assert "Connekt Flow" in welcome[0].subject
    assert "img/email/connektflow-wordmark-dark.png" in _html(welcome[0])
    # Greeted by the first name, never by the address.
    assert "Ana," in welcome[0].body
    assert "nova@example.com," not in welcome[0].body


@pytest.mark.django_db
def test_sign_up_asks_for_the_name(client):
    response = client.post(reverse("account_signup"), {"email": "nova@example.com", "password1": "Uma-senha-forte-42"})

    assert response.status_code == 200
    assert not User.objects.filter(email="nova@example.com").exists()


@pytest.mark.parametrize(
    ("first_name", "name", "email", "greeting"),
    [
        ("Ana", "Ana Souza", "a@x.com", "Ana"),
        ("", "Bia Lima", "b@x.com", "Bia"),
        ("", "", "caio.reis@x.com", "caio.reis"),
    ],
)
def test_the_e_mails_greet_by_the_first_name_then_the_name_then_the_address(first_name, name, email, greeting):
    assert User(first_name=first_name, name=name, email=email).greeting_name == greeting


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
