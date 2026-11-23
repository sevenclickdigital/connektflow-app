"""WhatsApp message templates: what is asked of the Cloud API and what is read back."""

from unittest.mock import MagicMock

from providers.whatsapp import WhatsAppProvider

TEMPLATES_URL = "https://graph.facebook.com/v25.0/waba-1/message_templates"


def _provider():
    return WhatsAppProvider(
        {"client_id": "id", "client_secret": "secret", "phone_number_id": "pn-1", "waba_id": "waba-1"}
    )


def _resp(body):
    return MagicMock(json=MagicMock(return_value=body))


def test_the_templates_are_read_page_by_page_with_their_body():
    provider = _provider()
    first = {
        "data": [
            {
                "id": "t-1",
                "name": "order_shipped",
                "status": "APPROVED",
                "category": "UTILITY",
                "language": "pt_BR",
                "components": [{"type": "HEADER", "text": "Pedido"}, {"type": "BODY", "text": "Seu pedido saiu."}],
            }
        ],
        "paging": {"cursors": {"after": "next"}},
    }
    second = {
        "data": [{"id": "t-2", "name": "welcome", "status": "PENDING", "category": "MARKETING", "language": "en_US"}]
    }
    provider._request = MagicMock(side_effect=[_resp(first), _resp(second)])

    templates = provider.list_message_templates("token")

    assert [(t.id, t.name, t.status, t.category, t.language, t.body) for t in templates] == [
        ("t-1", "order_shipped", "APPROVED", "UTILITY", "pt_BR", "Seu pedido saiu."),
        ("t-2", "welcome", "PENDING", "MARKETING", "en_US", ""),
    ]
    calls = provider._request.call_args_list
    assert [c.args[:2] for c in calls] == [("GET", TEMPLATES_URL), ("GET", TEMPLATES_URL)]
    assert "after" not in calls[0].kwargs["params"]
    assert calls[1].kwargs["params"]["after"] == "next"


def test_a_template_is_created_as_a_body_only_component():
    provider = _provider()
    provider._request = MagicMock(return_value=_resp({"id": "t-9", "status": "PENDING", "category": "MARKETING"}))

    template = provider.create_message_template(
        "token", name="order_shipped", language="pt_BR", category="UTILITY", body="Seu pedido saiu."
    )

    provider._request.assert_called_once_with(
        "POST",
        TEMPLATES_URL,
        access_token="token",
        json={
            "name": "order_shipped",
            "language": "pt_BR",
            "category": "UTILITY",
            "components": [{"type": "BODY", "text": "Seu pedido saiu."}],
        },
    )
    # Meta filed it under another category than the one asked for.
    assert (template.id, template.status, template.category) == ("t-9", "PENDING", "MARKETING")
