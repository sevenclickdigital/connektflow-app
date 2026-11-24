"""E-mail verification: the code a new account types before anything else, who
is spared it, when the welcome goes out, and the accounts that predate it."""

import importlib
import re
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.apps import apps as global_apps
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import Invitation, WorkspaceMembership
from apps.organizations.models import Organization

backfill = importlib.import_module("apps.accounts.migrations.0008_verify_existing_email_addresses")

FORM = {"full_name": "Camila Souza", "email": "camila@mac.example", "password1": "Uma-senha-forte-42"}
CODE_PAGE = "account_email_verification_sent"


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """allauth mails one code per address every few minutes, counted in the cache the tests share."""
    cache.clear()


def _sign_up(client, **overrides):
    return client.post(reverse("account_signup"), {**FORM, **overrides})


def _code_sent_to(address):
    message = [m for m in mail.outbox if m.to == [address]][-1]
    return re.search(r"^(\d{6})$", message.body, re.MULTILINE).group(1)


def _welcomes(address):
    return [m for m in mail.outbox if m.to == [address] and "account/email/welcome" in str(m.extra_headers)]


@pytest.mark.django_db
def test_a_new_account_types_the_code_before_it_gets_in(client, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        response = _sign_up(client)

    assert response.status_code == 302
    assert response["Location"] == reverse(CODE_PAGE)
    # Not signed in yet: the account's pages still ask for the login.
    assert client.get(reverse("dashboard")).status_code == 302
    assert "/accounts/login/" in client.get(reverse("dashboard"))["Location"]
    # Only the code has been sent; the welcome waits for the confirmation.
    assert [m.to for m in mail.outbox] == [["camila@mac.example"]]
    assert "Uma-senha" not in mail.outbox[0].body

    with django_capture_on_commit_callbacks(execute=True):
        confirmed = client.post(reverse(CODE_PAGE), {"code": _code_sent_to("camila@mac.example")})

    assert confirmed.status_code == 302
    assert confirmed["Location"] == reverse("dashboard")
    assert EmailAddress.objects.get(email="camila@mac.example").verified
    workspace = WorkspaceMembership.objects.get(user__email="camila@mac.example").workspace
    landing = client.get(confirmed["Location"], follow=True)
    assert landing.redirect_chain[-1][0] == f"{reverse('organizations:about')}?workspace={workspace.id}"
    # Welcomed once, now that the address is known to be theirs.
    assert len(mail.outbox) == 2
    assert "Camila," in mail.outbox[1].body


@pytest.mark.django_db
def test_a_wrong_code_keeps_the_account_out(client):
    _sign_up(client)

    response = client.post(reverse(CODE_PAGE), {"code": "000000"})

    assert response.status_code == 200
    assert not EmailAddress.objects.get(email="camila@mac.example").verified
    assert "/accounts/login/" in client.get(reverse("dashboard"))["Location"]


@pytest.mark.django_db
def test_an_unverified_account_is_asked_for_the_code_when_it_signs_in(client):
    User.objects.create_user(email="old@mac.example", password="Uma-senha-forte-42", tos_accepted_at=timezone.now())

    response = client.post(reverse("account_login"), {"login": "old@mac.example", "password": "Uma-senha-forte-42"})

    assert response.status_code == 302
    assert response["Location"] == reverse(CODE_PAGE)
    assert [m.to for m in mail.outbox] == [["old@mac.example"]]


@pytest.mark.django_db
def test_an_invitee_on_the_invited_address_is_not_asked(client, django_capture_on_commit_callbacks):
    invitation = Invitation.objects.create(
        organization=Organization.objects.create(name="Agency"),
        email="camila@mac.example",
        expires_at=timezone.now() + timedelta(days=7),
    )
    client.get(reverse("members:accept_invite", kwargs={"token": invitation.token}))

    with django_capture_on_commit_callbacks(execute=True):
        response = _sign_up(client)

    assert response["Location"] != reverse(CODE_PAGE)
    assert EmailAddress.objects.get(email="camila@mac.example").verified
    assert client.get(reverse("dashboard")).status_code != 302 or "/accounts/login/" not in client.get(
        reverse("dashboard")
    ).get("Location", "")
    # Nothing to confirm, so the welcome goes out with the sign-up.
    assert len([m for m in mail.outbox if m.to == ["camila@mac.example"]]) == 1


@pytest.mark.django_db
def test_an_invitee_who_signs_up_with_another_address_is_asked(client):
    invitation = Invitation.objects.create(
        organization=Organization.objects.create(name="Agency"),
        email="invited@mac.example",
        expires_at=timezone.now() + timedelta(days=7),
    )
    client.get(reverse("members:accept_invite", kwargs={"token": invitation.token}))

    response = _sign_up(client)

    assert response["Location"] == reverse(CODE_PAGE)
    assert not EmailAddress.objects.get(email="camila@mac.example").verified


@pytest.mark.django_db
def test_accounts_that_predate_verification_are_counted_as_verified():
    plain = User.objects.create_user(email="plain@mac.example", password="x")
    unverified = User.objects.create_user(email="unverified@mac.example", password="x")
    EmailAddress.objects.create(user=unverified, email="unverified@mac.example", primary=True, verified=False)
    # Two accounts whose addresses differ only in case: one verified row per address.
    upper = User.objects.create_user(email="Twin@mac.example", password="x")
    lower = User.objects.create_user(email="twin@mac.example", password="x")

    backfill.verify_existing_addresses(global_apps, None)
    backfill.verify_existing_addresses(global_apps, None)

    assert EmailAddress.objects.get(user=plain).verified
    assert EmailAddress.objects.get(user=plain).primary
    assert EmailAddress.objects.get(user=unverified).verified
    assert EmailAddress.objects.filter(user=unverified).count() == 1
    assert EmailAddress.objects.filter(user__in=[upper, lower], verified=True).count() == 1


@pytest.mark.django_db
def test_a_team_account_is_created_verified(client):
    from apps.accounts.operators import create_operator

    create_operator("suporte@mac.example", password="Uma-senha-forte-42")

    response = client.post(reverse("account_login"), {"login": "suporte@mac.example", "password": "Uma-senha-forte-42"})

    assert response.status_code == 302
    assert response["Location"] != reverse(CODE_PAGE)
    assert mail.outbox == []
