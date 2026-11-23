"""The message templates screen of a connected WhatsApp number."""

from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import APIError
from providers.types import MessageTemplate

VALID = {"name": "order_shipped", "category": "UTILITY", "language": "pt_BR", "body": "Seu pedido saiu."}


@pytest.fixture(autouse=True)
def _english_responses(settings):
    settings.LANGUAGE_CODE = "en-us"


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="WA WS", organization=organization)


@pytest.fixture
def number(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="whatsapp",
        account_platform_id="pn-1",
        webhook_target_id="waba-1",
        account_name="Loja",
        account_handle="+55 11 99999-0000",
        oauth_access_token="token",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def manager(client, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="manager")
    client.force_login(user)
    return client


@pytest.fixture
def provider():
    fake = MagicMock()
    with patch("apps.social_accounts.whatsapp_templates._account_provider", return_value=fake) as factory:
        fake.factory = factory
        yield fake


def _url(workspace, account):
    return reverse(
        "social_accounts:whatsapp_templates", kwargs={"workspace_id": workspace.id, "account_id": account.id}
    )


def test_the_page_shows_at_once_and_asks_meta_for_nothing(manager, workspace, number, provider):
    response = manager.get(_url(workspace, number))

    assert response.status_code == 200
    assert "partial=body" in response.content.decode()
    provider.list_message_templates.assert_not_called()


def test_the_list_shows_what_meta_holds_for_the_numbers_account(manager, workspace, number, provider):
    provider.list_message_templates.return_value = [
        MessageTemplate(
            id="t-1", name="order_shipped", language="pt_BR", category="UTILITY", status="APPROVED", body="Saiu."
        ),
        MessageTemplate(id="t-2", name="promo", language="xx", category="MARKETING", status="LIMIT_EXCEEDED"),
    ]

    body = manager.get(_url(workspace, number), {"partial": "body"}, headers={"HX-Request": "true"}).content.decode()

    assert provider.factory.call_args.args[0] == number
    provider.list_message_templates.assert_called_once_with("token")
    for text in ("order_shipped", "Approved", "Portuguese (Brazil)", "Saiu.", "promo", "Limit exceeded"):
        assert text in body


def test_a_failed_listing_says_so_instead_of_an_empty_list(manager, workspace, number, provider):
    provider.list_message_templates.side_effect = APIError("whatsapp API error 401", status_code=401)

    body = manager.get(_url(workspace, number), {"partial": "body"}, headers={"HX-Request": "true"}).content.decode()

    assert "Reconnect the number" in body
    assert "no templates yet" not in body


def test_a_template_is_created_and_the_list_is_told_to_reload(manager, workspace, number, provider):
    response = manager.post(_url(workspace, number), VALID)

    assert response.status_code == 200
    provider.create_message_template.assert_called_once_with("token", **VALID)
    assert "whatsappTemplatesChanged" in response.headers["HX-Trigger"]
    assert "Seu pedido saiu." not in response.content.decode()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "Order Shipped", "lowercase letters"),
        ("body", "Hi {{1}}", "Variables such as"),
        ("category", "AUTHENTICATION", "Select a valid choice"),
    ],
)
def test_what_meta_would_refuse_is_stopped_here(manager, workspace, number, provider, field, value, message):
    response = manager.post(_url(workspace, number), {**VALID, field: value})

    assert message in response.content.decode()
    assert "HX-Trigger" not in response.headers
    provider.create_message_template.assert_not_called()


def test_metas_own_explanation_of_a_refusal_is_shown(manager, workspace, number, provider):
    provider.create_message_template.side_effect = APIError(
        "whatsapp API error 400: {...}",
        status_code=400,
        raw_response={
            "error": {"code": 100, "error_user_msg": "There is already Portuguese (BR) content for this template."}
        },
    )

    response = manager.post(_url(workspace, number), VALID)

    body = response.content.decode()
    assert "There is already Portuguese (BR) content for this template." in body
    assert "Seu pedido saiu." in body
    assert "HX-Trigger" not in response.headers


def test_another_workspaces_number_is_not_found(manager, organization, workspace, number, user, provider):
    other = Workspace.objects.create(name="Other", organization=organization)
    WorkspaceMembership.objects.create(user=user, workspace=other, workspace_role="manager")

    assert manager.get(_url(other, number)).status_code == 404
    assert manager.post(_url(other, number), VALID).status_code == 404
    provider.create_message_template.assert_not_called()


def test_only_a_whatsapp_number_has_templates(manager, workspace, provider):
    page = SocialAccount.objects.create(
        workspace=workspace, platform="facebook", account_platform_id="p-1", account_name="Page"
    )

    assert manager.get(_url(workspace, page)).status_code == 404


def test_someone_who_cannot_manage_accounts_is_refused(client, django_user_model, workspace, number, provider):
    from django.utils import timezone

    viewer = django_user_model.objects.create_user(email="v@example.com", password="x", tos_accepted_at=timezone.now())
    WorkspaceMembership.objects.create(user=viewer, workspace=workspace, workspace_role="viewer")
    client.force_login(viewer)

    assert client.get(_url(workspace, number)).status_code == 403
    assert client.post(_url(workspace, number), VALID).status_code == 403
    provider.create_message_template.assert_not_called()
