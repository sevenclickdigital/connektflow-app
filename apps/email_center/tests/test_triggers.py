"""Who receives each automatic e-mail, when, and only once."""

from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.utils import timezone

from apps.contacts.models import Contact
from apps.email_center.models import AutomatedEmail, AutomatedEmailSend, AutomatedEmailText, Presence
from apps.email_center.triggers import send_due
from apps.notifications.models import Channel, EventType, NotificationPreference
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

NOW = timezone.now()


@pytest.fixture
def owner(org_owner, organization):
    EmailAddress.objects.create(user=org_owner, email=org_owner.email, verified=True, primary=True)
    type(org_owner).objects.filter(pk=org_owner.pk).update(created_at=NOW - timedelta(days=5))
    org_owner.refresh_from_db()
    Workspace.objects.create(organization=organization, name="Loja Centro")
    return org_owner


def _email(
    subject="{{ name }}, conecte suas redes", body="# Falta pouco\n\n[botão: Conectar]({{ connect_url }})", **fields
):
    defaults = {
        "name": "Conecte suas redes",
        "is_active": True,
        "activated_at": NOW - timedelta(days=30),
        "anchor": AutomatedEmail.Anchor.SIGNUP,
        "delay_days": 3,
    }
    email = AutomatedEmail.objects.create(**{**defaults, **fields})
    AutomatedEmailText.objects.create(automated_email=email, language="pt-br", subject=subject, body_markdown=body)
    return email


@pytest.mark.django_db
def test_sent_once_when_due_and_matching(owner):
    _email(connections=Presence.NONE)

    assert send_due(NOW) == 1
    assert send_due(NOW) == 0
    assert mail.outbox[-1].subject == "Test, conecte suas redes"
    assert "List-Unsubscribe" in mail.outbox[-1].extra_headers
    assert "/connect" in mail.outbox[-1].alternatives[0][0]


@pytest.mark.django_db
def test_not_before_the_day(owner):
    _email(delay_days=10)

    assert send_due(NOW) == 0


@pytest.mark.django_db
def test_a_condition_that_does_not_hold_stops_it(owner, organization):
    SocialAccount.objects.create(
        workspace=organization.workspaces.get(), platform="instagram_login", account_platform_id="ig1"
    )
    _email(connections=Presence.NONE)

    assert send_due(NOW) == 0


@pytest.mark.django_db
def test_contacts_the_inbox_records_are_not_tool_use(owner, organization):
    brand = organization.workspaces.get()
    Contact.objects.create(workspace=brand, name="Auto", source=Contact.Source.INSTAGRAM)
    _email(tools=Presence.NONE)
    assert send_due(NOW) == 1

    Contact.objects.create(workspace=brand, name="Manual", source=Contact.Source.MANUAL)
    _email(name="Usou ferramenta", tools=Presence.SOME)
    assert send_due(NOW) == 1


@pytest.mark.django_db
def test_setup_steps_bounds(owner):
    _email(setup_steps_min=1)
    assert send_due(NOW) == 0

    _email(name="Nada feito", setup_steps_max=0)
    assert send_due(NOW) == 1


@pytest.mark.django_db
def test_switching_on_does_not_mail_everyone_who_passed_the_day(owner):
    late = _email(activated_at=NOW)
    assert send_due(NOW) == 0

    late.include_overdue = True
    late.save(update_fields=["include_overdue"])
    assert send_due(NOW) == 1


@pytest.mark.django_db
def test_someone_who_turned_tips_off_gets_nothing(owner):
    NotificationPreference.objects.create(
        user=owner, event_type=EventType.LIFECYCLE_TIPS, channel=Channel.EMAIL, is_enabled=False
    )
    _email()

    assert send_due(NOW) == 0


@pytest.mark.django_db
def test_team_accounts_and_unconfirmed_addresses_are_skipped(owner):
    owner.is_staff = True
    owner.save(update_fields=["is_staff"])
    _email()
    assert send_due(NOW) == 0

    owner.is_staff = False
    owner.save(update_fields=["is_staff"])
    EmailAddress.objects.filter(user=owner).update(verified=False)
    assert send_due(NOW) == 0


@pytest.mark.django_db
def test_a_failed_send_is_tried_again(owner, monkeypatch):
    from apps.common.mail import EmailNotSentError

    _email()
    monkeypatch.setattr("apps.common.mail.send_or_raise", lambda message: (_ for _ in ()).throw(EmailNotSentError()))
    assert send_due(NOW) == 0
    assert not AutomatedEmailSend.objects.exists()

    monkeypatch.undo()
    assert send_due(NOW) == 1


@pytest.mark.django_db
def test_each_person_gets_the_text_in_their_language(owner):
    email = _email()
    AutomatedEmailText.objects.create(
        automated_email=email,
        language="en-us",
        subject="{{ name }}, connect your accounts",
        body_markdown="# Almost there",
    )
    owner.language = "en-us"
    owner.save(update_fields=["language"])

    send_due(NOW)

    assert mail.outbox[0].subject.endswith("connect your accounts")
    assert "Almost there" in mail.outbox[0].alternatives[0][0]
    assert "You're receiving this" in mail.outbox[0].body  # the footer follows the text's language


@pytest.mark.django_db
def test_a_language_without_its_own_text_gets_the_default_one(owner):
    _email()
    owner.language = "en-us"
    owner.save(update_fields=["language"])

    send_due(NOW)

    assert mail.outbox[0].subject.endswith("conecte suas redes")
    assert "Você está recebendo" in mail.outbox[0].body
