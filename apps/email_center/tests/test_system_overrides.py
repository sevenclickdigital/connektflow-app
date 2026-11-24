"""An active Markdown version replaces a system e-mail at both doors every e-mail goes through."""

from types import SimpleNamespace
from unittest import mock

import pytest
from django.utils import translation

from apps.accounts.adapters import AccountAdapter
from apps.common.emails import build_email
from apps.email_center.models import SystemEmailOverride

INVITE = "members/email/invite"
INVITE_CONTEXT = {
    "invitation": SimpleNamespace(name="Ana", email="ana@example.com"),
    "inviter_name": "Robison",
    "organization_name": "CAMILA AP",
    "brand": None,
    "accept_url": "https://app.example.com/accept/",
}


def _invite():
    with translation.override("pt-br"):
        return build_email(subject="Original", template_prefix=INVITE, context=INVITE_CONTEXT, to=["ana@example.com"])


@pytest.mark.django_db
def test_an_active_version_replaces_the_template():
    SystemEmailOverride.objects.create(
        key=INVITE,
        subject="Venha para {{ organization_name }}",
        body_markdown="# Oi {{ name }}\n\n{{ inviter_name }} te chamou.\n\n[botão: Aceitar]({{ accept_url }})",
        is_active=True,
    )

    message = _invite()
    html = message.alternatives[0][0]

    assert message.subject == "Venha para CAMILA AP"
    assert "Robison te chamou." in message.body
    assert "Oi Ana" in html and "https://app.example.com/accept/" in html


@pytest.mark.django_db
def test_a_draft_does_not_replace_the_template():
    SystemEmailOverride.objects.create(key=INVITE, subject="Rascunho", body_markdown="x", is_active=False)

    assert _invite().subject == "Original"


@pytest.mark.django_db
def test_a_version_that_fails_falls_back_to_the_template(caplog):
    SystemEmailOverride.objects.create(key=INVITE, subject="Quebrado", body_markdown="x", is_active=True)

    with mock.patch("apps.email_center.rendering.render_message", side_effect=RuntimeError("boom")):
        message = _invite()

    assert message.subject == "Original"
    assert "failed to render" in caplog.text


@pytest.mark.django_db
def test_allauth_mail_uses_the_active_version(rf, user):
    SystemEmailOverride.objects.create(
        key="account/email/login_code", subject="Seu código", body_markdown="Código: **{{ code }}**", is_active=True
    )
    request = rf.get("/")
    request.user = user

    message = AccountAdapter(request).render_mail("account/email/login_code", user.email, {"code": "482913"})

    assert message.subject == "Seu código"
    assert "Código: 482913" in message.body
    assert message.to == [user.email]


@pytest.mark.django_db
def test_an_edited_version_keeps_the_default_footer_reason():
    SystemEmailOverride.objects.create(key=INVITE, subject="Oi", body_markdown="Texto", is_active=True)

    html = _invite().alternatives[0][0]

    assert "ana@example.com" in html.split("<!--cf-reason-->")[1].split("<!--/cf-reason-->")[0]


@pytest.mark.django_db
def test_a_version_is_used_only_in_its_own_language():
    SystemEmailOverride.objects.create(
        key=INVITE, language="pt-br", subject="Venha para {{ organization_name }}", body_markdown="Oi", is_active=True
    )

    with translation.override("en-us"):
        message = build_email(subject="Original", template_prefix=INVITE, context=INVITE_CONTEXT, to=["a@example.com"])

    assert message.subject == "Original"


@pytest.mark.django_db
def test_the_english_version_goes_to_english_sends():
    SystemEmailOverride.objects.create(
        key=INVITE, language="en-us", subject="Join {{ organization_name }}", body_markdown="Hi", is_active=True
    )

    with translation.override("en-us"):
        message = build_email(subject="Original", template_prefix=INVITE, context=INVITE_CONTEXT, to=["a@example.com"])

    assert message.subject == "Join CAMILA AP"
    assert _invite().subject == "Original"


@pytest.mark.django_db
def test_a_system_email_goes_out_in_the_language_of_the_recipients_profile(django_user_model):
    django_user_model.objects.create_user(email="ana@example.com", password="x-Strong-pass-123", language="en-us")

    message = _invite()  # built while the sender's page is in Portuguese

    assert "Invitation to the organization CAMILA AP" in message.alternatives[0][0]
    assert "Robison invited you" in message.body


@pytest.mark.django_db
def test_allauth_mail_goes_out_in_the_recipients_language(rf, user):
    user.language = "en-us"
    user.save(update_fields=["language"])
    request = rf.get("/")
    request.user = user

    with translation.override("pt-br"):
        message = AccountAdapter(request).render_mail("account/email/password_changed", user.email, {"user": user})

    assert "password" in message.subject.lower()
    assert "senha" not in message.subject.lower()
