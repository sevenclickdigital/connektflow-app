"""The e-mail screens answer to superadmins only, and save what they are given."""

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.operators import create_operator
from apps.email_center.models import AutomatedEmail, SystemEmailOverride

INVITE = "members/email/invite"


@pytest.fixture
def superadmin(db):
    client = Client()
    client.force_login(create_operator("root@example.com", superuser=True))
    return client


@pytest.mark.django_db
def test_support_staff_cannot_open_it():
    client = Client()
    client.force_login(create_operator("support@example.com"))

    assert client.get(reverse("email_center_staff:index")).status_code == 403
    response = client.post(
        reverse("email_center_staff:system_edit", kwargs={"key": INVITE}),
        {"subject": "x", "body_markdown": "x", "is_active": "on"},
    )
    assert response.status_code == 403
    assert not SystemEmailOverride.objects.exists()


@pytest.mark.django_db
def test_every_screen_opens(superadmin):
    email = AutomatedEmail.objects.create(name="A", subject="s", body_markdown="b")

    for url in (
        reverse("email_center_staff:index"),
        reverse("email_center_staff:system_edit", kwargs={"key": INVITE}),
        reverse("email_center_staff:system_edit", kwargs={"key": "account/email/login_code"}),
        reverse("email_center_staff:automated_new"),
        reverse("email_center_staff:automated_edit", kwargs={"email_id": email.id}),
    ):
        assert superadmin.get(url).status_code == 200, url


@pytest.mark.django_db
def test_saving_a_system_version_and_restoring_the_default(superadmin):
    url = reverse("email_center_staff:system_edit", kwargs={"key": INVITE})
    superadmin.post(url, {"subject": "Oi {{ name }}", "body_markdown": "# Oi", "is_active": "on"})

    override = SystemEmailOverride.objects.get(key=INVITE)
    assert override.is_active and override.subject == "Oi {{ name }}"

    superadmin.post(reverse("email_center_staff:system_reset", kwargs={"key": INVITE}))
    assert not SystemEmailOverride.objects.exists()


@pytest.mark.django_db
def test_an_unknown_variable_is_refused(superadmin):
    url = reverse("email_center_staff:system_edit", kwargs={"key": INVITE})
    response = superadmin.post(url, {"subject": "Oi", "body_markdown": "Olá {{ nme }}", "is_active": "on"})

    assert response.status_code == 200
    assert "nme" in response.content.decode()
    assert not SystemEmailOverride.objects.exists()


@pytest.mark.django_db
def test_an_unknown_system_email_is_not_found(superadmin):
    assert superadmin.get("/staff/emails/system/does/not/exist/").status_code == 404


@pytest.mark.django_db
def test_switching_an_automatic_email_on_starts_its_clock(superadmin):
    response = superadmin.post(
        reverse("email_center_staff:automated_new"),
        {
            "name": "Boas-vindas D+3",
            "subject": "Oi {{ name }}",
            "body_markdown": "# Oi",
            "anchor": "signup",
            "delay_days": 3,
            "connections": "none",
            "posts": "any",
            "tasks": "any",
            "tools": "any",
            "is_active": "on",
        },
    )

    email = AutomatedEmail.objects.get()
    assert response.status_code == 302
    assert email.is_active and email.activated_at is not None


@pytest.mark.django_db
def test_the_preview_is_sanitized(superadmin):
    response = superadmin.post(
        reverse("email_center_staff:preview"),
        {"subject": "Oi", "body_markdown": "# Oi {{ name }}\n\n<script>alert(1)</script>"},
    )

    body = response.content.decode()
    assert response.status_code == 200
    assert "sandbox" in body
    # The whole e-mail sits inside srcdoc="…": unescaped, its own quotes would cut the iframe short.
    assert 'srcdoc="&lt;!DOCTYPE html&gt;' in body
    assert "<script>alert" not in body and "&lt;script&gt;alert" not in body


@pytest.mark.django_db
def test_the_editor_starts_from_the_default_text(superadmin):
    response = superadmin.get(
        reverse("email_center_staff:system_edit", kwargs={"key": "organizations/email/deletion_scheduled"})
    )

    form = response.context["form"]
    assert "{{ organization_name }}" in form.initial["subject"]
    assert "[botão: Cancelar a exclusão]({{ restore_url }})" in form.initial["body_markdown"]


@pytest.mark.django_db
def test_the_list_switches_an_automatic_email_on_and_off(superadmin):
    email = AutomatedEmail.objects.create(name="A", subject="s", body_markdown="b")
    url = reverse("email_center_staff:automated_toggle", kwargs={"email_id": email.id})

    assert superadmin.post(url).status_code == 302
    email.refresh_from_db()
    assert email.is_active and email.activated_at is not None

    superadmin.post(url)
    email.refresh_from_db()
    assert not email.is_active


@pytest.mark.django_db
def test_support_staff_cannot_switch_an_automatic_email():
    email = AutomatedEmail.objects.create(name="A", subject="s", body_markdown="b")
    client = Client()
    client.force_login(create_operator("support@example.com"))

    response = client.post(reverse("email_center_staff:automated_toggle", kwargs={"email_id": email.id}))

    assert response.status_code == 403
    email.refresh_from_db()
    assert not email.is_active
