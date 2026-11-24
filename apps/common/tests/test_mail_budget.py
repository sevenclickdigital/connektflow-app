"""The outbound-email budget: the ceiling that makes a send storm impossible.

Django's setup_test_environment() pins EMAIL_BACKEND to locmem for the whole
run, so every test here has to ask for the wrapper back explicitly. ``budgeted``
does that and sets the limits the test cares about.
"""

import datetime
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings
from django.utils import timezone

from apps.common.mail import CLASS_NOTIFICATION, EMAIL_CLASS_HEADER, purge_expired_counters, transactional
from apps.common.models import EmailSendCounter, EmailSuppression

BACKEND = "apps.common.mail.BudgetedEmailBackend"


def budgeted(**limits):
    """Turn the wrapper on, with every limit unlimited unless named."""
    defaults = {
        "EMAIL_BACKEND": BACKEND,
        "EMAIL_INNER_BACKEND": "django.core.mail.backends.locmem.EmailBackend",
        "EMAIL_SENDING_ENABLED": True,
        "EMAIL_DAILY_SEND_LIMIT": -1,
        "EMAIL_RECIPIENT_HOURLY_LIMIT": -1,
        "EMAIL_RECIPIENT_DAILY_LIMIT": -1,
    }
    defaults.update(limits)
    return override_settings(**defaults)


def send(to="someone@example.com", subject="Hello", headers=None):
    msg = EmailMultiAlternatives(
        subject=subject, body="body", from_email="noreply@example.com", to=[to], headers=headers
    )
    return msg.send(fail_silently=False)


@pytest.mark.django_db
def test_an_ordinary_send_goes_through_and_is_counted():
    with budgeted():
        assert send() == 1
    assert len(mail.outbox) == 1
    assert EmailSendCounter.objects.get(scope="global_day").count == 1


@pytest.mark.django_db
def test_the_kill_switch_drops_everything():
    with budgeted(EMAIL_SENDING_ENABLED=False):
        assert send() == 0
    assert mail.outbox == []
    # Nothing was sent, so nothing was charged.
    assert not EmailSendCounter.objects.exists()


@pytest.mark.django_db
def test_the_per_recipient_hourly_cap_stops_a_storm():
    with budgeted(EMAIL_RECIPIENT_HOURLY_LIMIT=6):
        sent = sum(send(subject=f"n{i}") for i in range(10))
    assert sent == 6
    assert len(mail.outbox) == 6


@pytest.mark.django_db
def test_a_transactional_message_still_reaches_a_capped_recipient():
    """The whole point of the class split: a notification storm must never lock
    someone out of their own password reset."""
    with budgeted(EMAIL_RECIPIENT_HOURLY_LIMIT=2):
        for i in range(5):
            send(subject=f"noise {i}")
        assert len(mail.outbox) == 2

        assert send(subject="Password Reset Email", headers=transactional()) == 1

    assert len(mail.outbox) == 3
    assert mail.outbox[-1].subject == "Password Reset Email"


@pytest.mark.django_db
def test_the_global_cap_stops_even_transactional_mail():
    with budgeted(EMAIL_DAILY_SEND_LIMIT=3):
        for i in range(5):
            send(to=f"person{i}@example.com", headers=transactional())
    assert len(mail.outbox) == 3
    assert EmailSendCounter.objects.get(scope="global_day").count == 3


@pytest.mark.django_db
def test_the_global_counter_rolls_at_utc_midnight():
    """A stale bucket must not spend today's budget."""
    yesterday = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0) - datetime.timedelta(days=1)
    EmailSendCounter.objects.create(scope="global_day", key="", period_start=yesterday, count=999)

    with budgeted(EMAIL_DAILY_SEND_LIMIT=2):
        assert send() == 1

    assert EmailSendCounter.objects.get(scope="global_day", period_start__gt=yesterday).count == 1


@pytest.mark.django_db
def test_a_suppressed_address_receives_nothing():
    EmailSuppression.objects.create(address="dead@example.com", reason=EmailSuppression.Reason.BOUNCED)
    with budgeted():
        assert send(to="dead@example.com") == 0
        assert send(to="DEAD@example.com") == 0, "suppression must not be case-sensitive"
        assert send(to="alive@example.com") == 1
    assert [m.to for m in mail.outbox] == [["alive@example.com"]]


@pytest.mark.django_db
def test_the_class_header_never_reaches_the_recipient():
    with budgeted():
        send(headers=transactional())
    # The serialized message is what actually goes on the wire.
    assert EMAIL_CLASS_HEADER not in mail.outbox[0].message()


@pytest.mark.django_db
def test_a_dropped_message_keeps_its_class():
    """The header is stripped from survivors only. Stripping during the decision
    would hand a dropped message back unclassified, and anything re-sending it
    would silently demote a password reset to a notification."""
    msg = EmailMultiAlternatives(
        subject="Password Reset Email",
        body="b",
        from_email="noreply@example.com",
        to=["someone@example.com"],
        headers=transactional(),
    )
    with budgeted(EMAIL_DAILY_SEND_LIMIT=0):
        assert msg.send(fail_silently=False) == 0

    assert msg.extra_headers[EMAIL_CLASS_HEADER] == "transactional"


@pytest.mark.django_db
def test_recipients_are_counted_by_hash_not_by_address():
    with budgeted(EMAIL_RECIPIENT_HOURLY_LIMIT=5):
        send(to="private@example.com")
    keys = list(EmailSendCounter.objects.filter(scope="recipient_hour").values_list("key", flat=True))
    assert keys and "private@example.com" not in keys[0]


@pytest.mark.django_db
def test_unlimited_still_counts():
    """A negative limit means 'do not stop me', not 'do not measure me' — the
    count is how we learn where the limit belongs."""
    with budgeted(EMAIL_DAILY_SEND_LIMIT=-1):
        for i in range(4):
            send(subject=f"n{i}")
    assert EmailSendCounter.objects.get(scope="global_day").count == 4


@pytest.mark.django_db
def test_notification_is_the_default_class():
    with budgeted(EMAIL_RECIPIENT_HOURLY_LIMIT=1):
        assert send() == 1
        assert send(headers={EMAIL_CLASS_HEADER: CLASS_NOTIFICATION}) == 0


@pytest.mark.django_db
def test_purge_drops_only_old_buckets():
    now = timezone.now()
    EmailSendCounter.objects.create(scope="global_day", key="", period_start=now - datetime.timedelta(days=30))
    EmailSendCounter.objects.create(scope="global_day", key="a", period_start=now)
    assert purge_expired_counters(older_than_days=7) == 1
    assert EmailSendCounter.objects.count() == 1


@pytest.mark.django_db
def test_a_drop_does_not_burn_the_other_budgets():
    """A message stopped by one cap must not spend the others.

    Otherwise every drop inflates the counters, and the caps bite harder and
    harder during exactly the storm they exist to contain — for mail nobody
    ever received.
    """
    with budgeted(EMAIL_RECIPIENT_HOURLY_LIMIT=10, EMAIL_DAILY_SEND_LIMIT=1):
        for i in range(3):
            send(subject=f"n{i}")

    assert len(mail.outbox) == 1
    assert EmailSendCounter.objects.get(scope="recipient_hour").count == 1
    assert EmailSendCounter.objects.get(scope="recipient_day").count == 1
    assert EmailSendCounter.objects.get(scope="global_day").count == 1


@pytest.mark.django_db
def test_a_limit_of_zero_means_zero():
    with budgeted(EMAIL_DAILY_SEND_LIMIT=0):
        assert send() == 0
    assert mail.outbox == []


@pytest.mark.django_db
def test_a_bcc_does_not_spend_the_addressees_budget():
    with budgeted(EMAIL_RECIPIENT_HOURLY_LIMIT=5):
        msg = EmailMultiAlternatives(subject="s", body="b", from_email="noreply@example.com", to=["them@example.com"])
        msg.bcc = ["audit@example.com"]
        assert msg.send(fail_silently=False) == 1

    assert EmailSendCounter.objects.filter(scope="recipient_hour").count() == 1


@pytest.mark.django_db
def test_a_broken_counter_table_stops_notification_mail():
    """A send storm is what exhausts the connection pool, so the moment this
    query starts failing is the moment the cap matters most. Failing open there
    would lift the ceiling during the incident it exists to contain."""
    from django.db import DatabaseError

    with (
        budgeted(EMAIL_DAILY_SEND_LIMIT=1000),
        patch(
            "apps.common.models.EmailSendCounter.objects.get_or_create",
            side_effect=DatabaseError("pool exhausted"),
        ),
    ):
        assert send(subject="Post failed to publish") == 0

    assert mail.outbox == []


@pytest.mark.django_db
def test_a_broken_counter_table_still_lets_a_password_reset_through():
    """The other side of the same call: a counter table nobody can read must
    not lock someone out of their own account."""
    from django.db import DatabaseError

    with (
        budgeted(EMAIL_DAILY_SEND_LIMIT=1000),
        patch(
            "apps.common.models.EmailSendCounter.objects.get_or_create",
            side_effect=DatabaseError("pool exhausted"),
        ),
    ):
        assert send(subject="Password Reset Email", headers=transactional()) == 1

    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_what_is_sent_and_what_is_stopped_goes_in_the_history_without_its_content():
    from apps.common.models import EmailLog

    EmailSuppression.objects.create(address="dead@example.com", reason=EmailSuppression.Reason.BOUNCED)
    with budgeted():
        send(to="Ana@Example.com", subject="Your code", headers=transactional())
        send(to="dead@example.com", subject="Weekly digest")

    sent = EmailLog.objects.get(recipient="ana@example.com")
    assert (sent.subject, sent.status, sent.email_class) == ("Your code", "sent", "transactional")
    stopped = EmailLog.objects.get(recipient="dead@example.com")
    assert (stopped.status, stopped.detail, stopped.email_class) == (
        "dropped",
        "recipient suppressed",
        CLASS_NOTIFICATION,
    )
    # Addressee, subject and outcome only: no column holds what the mail said.
    assert "body" not in str(list(EmailLog.objects.values()))


@pytest.mark.django_db
def test_a_refusal_of_the_mail_server_is_recorded_and_still_raised(monkeypatch):
    from django.core.mail.backends.locmem import EmailBackend

    from apps.common.models import EmailLog

    def refuse(self, messages):
        raise ConnectionError("smtp down")

    monkeypatch.setattr(EmailBackend, "send_messages", refuse)
    with budgeted(), pytest.raises(ConnectionError):
        send(to="ana@example.com")

    assert EmailLog.objects.get(recipient="ana@example.com").status == "failed"


@pytest.mark.django_db
def test_the_history_is_forgotten_after_its_retention():
    from datetime import timedelta

    from apps.common.mail import EMAIL_LOG_RETENTION_DAYS, purge_old_email_logs
    from apps.common.models import EmailLog

    old = EmailLog.objects.create(recipient="a@example.com", subject="Old", status="sent")
    EmailLog.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=EMAIL_LOG_RETENTION_DAYS + 1))
    EmailLog.objects.create(recipient="a@example.com", subject="Recent", status="sent")

    assert purge_old_email_logs() == 1
    assert list(EmailLog.objects.values_list("subject", flat=True)) == ["Recent"]
