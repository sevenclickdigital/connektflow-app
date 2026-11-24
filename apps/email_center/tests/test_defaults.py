"""The editor starts every system e-mail from its default text, converted to Markdown."""

import pytest
from django.utils import translation

from apps.email_center import catalog, defaults, markdown_mail


@pytest.mark.parametrize("entry", catalog.SYSTEM_EMAILS, ids=lambda entry: entry.key)
def test_every_default_converts_with_only_its_own_variables(entry):
    with translation.override("pt-br"):
        subject, body = defaults.default_version(entry)

    assert subject.strip() and body.strip()
    assert markdown_mail.variables_used(subject, body) <= set(catalog.all_variables(entry))
    # Addresses of the environment that rendered it must not be baked into the text.
    assert "localhost" not in body and "example.com" not in body


def test_the_default_keeps_the_layout_pieces():
    with translation.override("pt-br"):
        _subject, body = defaults.default_version(catalog.get("account/email/password_changed"))

    assert body.startswith("# Sua senha foi alterada")
    assert "> <small>**De onde veio a alteração**" in body  # the compact box of the security notices
    assert "[botão: Redefinir minha senha]({{ reset_password_url }})" in body
    assert "Se o botão não funcionar" not in body  # the button adds its own fallback line


def test_the_default_renders_back_into_the_layout():
    entry = catalog.get("account/email/password_changed")
    with translation.override("pt-br"):
        _subject, body = defaults.default_version(entry)
        rendered = markdown_mail.render(body, catalog.resolve(entry, entry.sample()))

    assert 'class="cf-btn"' in rendered.html and "padding:14px 18px;background-color:#F5F3FF" in rendered.html
