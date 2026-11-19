"""WhatsApp Cloud API: messages arrive by webhook, replies respect the 24-hour
window, failed deliveries are visible, and nothing crosses tenants."""

import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import InboxMessage, InboxReply
from apps.inbox.reply_policy import reply_eligibility
from apps.inbox.services import _dispatch_to_platform
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from providers.exceptions import MessagingError
from providers.whatsapp import WhatsAppProvider

SECRET = "wa-secret"
CREDS = {"whatsapp": {"app_id": "app", "app_secret": SECRET, "config_id": "cfg"}}
WEBHOOK = "inbox_webhooks:webhook_whatsapp"


@pytest.fixture(autouse=True)
def _whatsapp_app(settings):
    settings.PLATFORM_CREDENTIALS_FROM_ENV = CREDS
    settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN = "verify-me"


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="WA WS", organization=organization)


@pytest.fixture
def wa_account(db, workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="whatsapp",
        account_platform_id="1001",
        webhook_target_id="2002",
        account_name="Loja",
        account_handle="+55 11 99999-0000",
        oauth_access_token="biz-token",
    )


def _post(client, payload, secret=SECRET):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(reverse(WEBHOOK), data=body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=signature)


def _payload(*, waba="2002", phone="1001", messages=None, statuses=None, contacts=None):
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": phone}}
    if messages is not None:
        value["messages"] = messages
        value["contacts"] = contacts or [{"wa_id": "5511988887777", "profile": {"name": "Rita"}}]
    if statuses is not None:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": waba, "changes": [{"field": "messages", "value": value}]}],
    }


def _text(message_id="wamid.1", body="Oi, tem estoque?", minutes_ago=1):
    ts = int((timezone.now() - timedelta(minutes=minutes_ago)).timestamp())
    return {"from": "5511988887777", "id": message_id, "timestamp": str(ts), "type": "text", "text": {"body": body}}


@pytest.mark.django_db
class TestWebhook:
    def test_handshake_echoes_the_challenge(self, client):
        response = client.get(
            reverse(WEBHOOK), {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"}
        )
        assert response.content == b"42"

    def test_an_unsigned_delivery_is_refused(self, client, wa_account):
        response = _post(client, _payload(messages=[_text()]), secret="forged")
        assert response.status_code == 403
        assert not InboxMessage.objects.exists()

    def test_a_text_message_lands_in_the_inbox_once(self, client, wa_account):
        _post(client, _payload(messages=[_text()]))
        _post(client, _payload(messages=[_text()]))  # Meta redelivers

        message = InboxMessage.objects.get()
        assert message.social_account == wa_account
        assert message.message_type == InboxMessage.MessageType.DM
        assert message.body == "Oi, tem estoque?"
        assert message.sender_name == "Rita"
        assert message.sender_handle == "5511988887777"

    def test_a_number_under_another_business_account_is_ignored(self, client, wa_account, caplog):
        with caplog.at_level("WARNING", logger="apps.inbox.webhooks"):
            _post(client, _payload(waba="9999", messages=[_text()]))

        assert not InboxMessage.objects.exists()
        assert "no connected account for number 1001 of business account 9999" in caplog.text

    def test_media_keeps_its_caption_and_media_id(self, client, wa_account):
        image = {
            **_text(),
            "type": "image",
            "image": {"id": "777", "caption": "Esse modelo", "mime_type": "image/jpeg"},
        }
        image.pop("text")
        _post(client, _payload(messages=[image]))

        message = InboxMessage.objects.get()
        assert message.body == "Esse modelo"
        assert message.extra["attachments"] == [{"type": "image", "url": "", "media_id": "777"}]

    def test_a_failed_delivery_marks_the_reply_failed(self, client, wa_account):
        message = InboxMessage.objects.create(
            workspace=wa_account.workspace,
            social_account=wa_account,
            platform_message_id="wamid.in",
            message_type="dm",
            sender_name="Rita",
            sender_handle="5511988887777",
            received_at=timezone.now(),
        )
        reply = InboxReply.objects.create(
            inbox_message=message, body="Temos sim", status=InboxReply.Status.SENT, platform_reply_id="wamid.out"
        )
        _post(
            client,
            _payload(statuses=[{"id": "wamid.out", "status": "failed", "errors": [{"code": 131047, "title": "x"}]}]),
        )

        reply.refresh_from_db()
        assert reply.status == InboxReply.Status.FAILED
        assert "24" in reply.send_error


@pytest.mark.django_db
class TestReplyWindow:
    def _message(self, account, hours_ago):
        return InboxMessage.objects.create(
            workspace=account.workspace,
            social_account=account,
            platform_message_id=f"wamid.{hours_ago}",
            message_type="dm",
            sender_name="Rita",
            sender_handle="5511988887777",
            extra={"sender_id": "5511988887777"},
            received_at=timezone.now() - timedelta(hours=hours_ago),
        )

    def test_inside_24_hours_text_can_be_sent(self, wa_account):
        assert reply_eligibility(self._message(wa_account, 2))["can_send"] is True

    def test_after_24_hours_it_is_refused_with_the_reason(self, wa_account):
        policy = reply_eligibility(self._message(wa_account, 30))
        assert policy["can_send"] is False
        assert policy["window"] == "expired"

    def test_a_newer_customer_message_reopens_the_window(self, wa_account):
        old = self._message(wa_account, 30)
        self._message(wa_account, 1)
        assert reply_eligibility(old)["can_send"] is True

    def test_the_reply_goes_to_the_customer_number_as_a_plain_message(self, wa_account):
        message = self._message(wa_account, 1)
        response = MagicMock(status_code=200)
        response.json.return_value = {"messages": [{"id": "wamid.out"}]}
        with patch.object(WhatsAppProvider, "_request", return_value=response) as request:
            assert _dispatch_to_platform(message, "Temos sim!") == "wamid.out"

        method, url = request.call_args.args
        payload = request.call_args.kwargs["json"]
        assert (method, url.rsplit("/", 2)[-2:]) == ("POST", ["1001", "messages"])
        assert payload["to"] == "5511988887777"
        assert payload["text"]["body"] == "Temos sim!"
        assert "context" not in payload


class TestProviderErrors:
    def test_the_24_hour_refusal_is_shown_as_a_sentence(self):
        response = httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})
        error = WhatsAppProvider({})._error_for_response(response)
        assert isinstance(error, MessagingError)
        assert "24" in str(error)

    def test_a_display_name_awaiting_approval_is_named_as_the_cause(self):
        response = httpx.Response(400, json={"error": {"code": 131037, "message": "needs display name approval"}})
        error = WhatsAppProvider({})._error_for_response(response)
        assert isinstance(error, MessagingError)
        assert "Gerenciador do WhatsApp" in str(error)


@pytest.mark.django_db
class TestConnect:
    @pytest.fixture
    def manager_client(self, client, user, workspace):
        OrgMembership.objects.create(
            user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER
        )
        WorkspaceMembership.objects.create(
            user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        client.force_login(user)
        return client

    def _connect(self, client, workspace, **data):
        form = {"code": "c0de", "waba_id": "2002", "phone_number_id": "1001", **data}
        return client.post(reverse("social_accounts:connect_whatsapp", kwargs={"workspace_id": workspace.id}), form)

    def test_the_page_offers_embedded_signup_without_the_secret(self, manager_client, workspace):
        response = manager_client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id}))

        assert b"data-whatsapp-signup" in response.content
        assert b'"config_id": "cfg"' in response.content
        assert SECRET.encode() not in response.content
        policy = response["Content-Security-Policy-Report-Only"]
        directives = {d.split()[0]: d for d in policy.split(";") if d.strip()}
        assert "https://connect.facebook.net" in directives["script-src"]
        assert "https://connect.facebook.net" in directives["connect-src"]

    def test_the_page_keeps_the_link_to_the_popup_meta_answers_through(self, manager_client, workspace):
        response = manager_client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id}))

        assert response["Cross-Origin-Opener-Policy"] == "same-origin-allow-popups"

    def test_the_popup_asks_for_the_current_signup_version_without_business_app_numbers(
        self, manager_client, workspace
    ):
        from providers.whatsapp import EMBEDDED_SIGNUP_VERSION

        response = manager_client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id}))

        body = response.content.decode()
        assert f'"signup_version": "{EMBEDDED_SIGNUP_VERSION}"' in body
        assert "name: 'whatsapp_business_app_onboarding', enabled: false" in body
        assert "sessionInfoVersion" not in body

    def test_a_number_outside_the_authorized_account_is_refused(self, manager_client, workspace):
        with (
            patch.object(WhatsAppProvider, "exchange_code", return_value=MagicMock(access_token="t", expires_in=None)),
            patch.object(WhatsAppProvider, "waba_phone_number_ids", return_value={"5555"}),
            patch.object(WhatsAppProvider, "ensure_registered") as register,
        ):
            self._connect(manager_client, workspace)

        register.assert_not_called()
        assert not SocialAccount.objects.filter(platform="whatsapp").exists()

    def test_a_connection_that_does_not_happen_says_why_in_the_log(self, manager_client, workspace, caplog):
        with caplog.at_level("WARNING", logger="apps.social_accounts.views"):
            self._connect(manager_client, workspace, waba_id="")
            with (
                patch.object(
                    WhatsAppProvider, "exchange_code", return_value=MagicMock(access_token="t", expires_in=None)
                ),
                patch.object(WhatsAppProvider, "waba_phone_number_ids", return_value={"5555"}),
            ):
                self._connect(manager_client, workspace)

        incomplete, mismatch = (record.getMessage() for record in caplog.records)
        assert "came back incomplete" in incomplete and "waba_id=''" in incomplete
        assert "number 1001 is not among the 1 numbers of account 2002 (5555)" in mismatch
        assert not SocialAccount.objects.filter(platform="whatsapp").exists()

    def test_connecting_creates_a_chat_only_account(self, manager_client, workspace):
        from providers.types import AccountProfile

        profile = AccountProfile(platform_id="1001", name="Loja", handle="+55 11 99999-0000")
        with (
            patch.object(WhatsAppProvider, "exchange_code", return_value=MagicMock(access_token="t", expires_in=None)),
            patch.object(WhatsAppProvider, "waba_phone_number_ids", return_value={"1001"}),
            patch.object(WhatsAppProvider, "ensure_registered"),
            patch.object(WhatsAppProvider, "get_profile", return_value=profile),
            patch("apps.social_accounts.views.subscribe_account_webhooks_task"),
        ):
            self._connect(manager_client, workspace)

        account = SocialAccount.objects.get(platform="whatsapp")
        assert (account.account_platform_id, account.webhook_target_id) == ("1001", "2002")
        assert not account.posting_slots.exists()
        assert not SocialAccount.objects.publishable().filter(pk=account.pk).exists()


@pytest.mark.django_db
class TestMediaProxy:
    def test_another_workspace_cannot_fetch_the_media(self, client, wa_account, django_user_model):
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        message = InboxMessage.objects.create(
            workspace=wa_account.workspace,
            social_account=wa_account,
            platform_message_id="wamid.img",
            message_type="dm",
            sender_name="Rita",
            extra={"attachments": [{"type": "image", "url": "", "media_id": "777"}]},
            received_at=timezone.now(),
        )
        outsider = django_user_model.objects.create_user(
            email="out@x.com", password="x", tos_accepted_at=timezone.now()
        )
        other = Workspace.objects.create(name="Other", organization=Organization.objects.create(name="Other org"))
        OrgMembership.objects.create(
            user=outsider, organization=other.organization, org_role=OrgMembership.OrgRole.OWNER
        )
        WorkspaceMembership.objects.create(
            user=outsider, workspace=other, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        client.force_login(outsider)

        with patch.object(WhatsAppProvider, "download_media") as download:
            response = client.get(
                reverse("inbox:message_media", kwargs={"workspace_id": other.id, "message_id": message.id, "index": 0})
            )

        assert response.status_code == 404
        download.assert_not_called()


@pytest.mark.django_db
class TestLiveInbox:
    @pytest.fixture
    def member_client(self, client, user, workspace):
        OrgMembership.objects.create(
            user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER
        )
        WorkspaceMembership.objects.create(
            user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        client.force_login(user)
        return client

    def _dm(self, account, message_id, minutes_ago, sender="5511988887777"):
        return InboxMessage.objects.create(
            workspace=account.workspace,
            social_account=account,
            platform_message_id=message_id,
            message_type="dm",
            sender_name="Rita",
            sender_handle=sender,
            body=message_id,
            received_at=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def test_the_version_moves_when_a_message_lands(self, member_client, wa_account, workspace):
        url = reverse("inbox:version", kwargs={"workspace_id": workspace.id})
        before = member_client.get(url).json()["v"]
        self._dm(wa_account, "wamid.new", 0)

        assert member_client.get(url).json()["v"] != before

    def test_the_version_of_another_workspace_is_refused(self, member_client):
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(name="Other", organization=Organization.objects.create(name="Other org"))
        response = member_client.get(reverse("inbox:version", kwargs={"workspace_id": other.id}))

        assert response.status_code in (403, 404)

    def test_the_panel_shows_the_earlier_messages_of_that_person_only(self, member_client, wa_account, workspace):
        self._dm(wa_account, "earlier-from-rita", 30)
        self._dm(wa_account, "from-someone-else", 20, sender="5521900000000")
        current = self._dm(wa_account, "current-from-rita", 1)

        response = member_client.get(
            reverse("inbox:message_detail", kwargs={"workspace_id": workspace.id, "message_id": current.id}),
            HTTP_HX_REQUEST="true",
        )

        incoming = [item["obj"].body for item in response.context["chat"] if item["kind"] == "in"]
        assert incoming == ["earlier-from-rita", "current-from-rita"]
        assert b"from-someone-else" not in response.content


@pytest.mark.django_db
class TestMessageActions:
    """Quote, react and reactions received, on the bubbles of a WhatsApp chat."""

    @pytest.fixture
    def member_client(self, client, user, workspace):
        OrgMembership.objects.create(
            user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER
        )
        WorkspaceMembership.objects.create(
            user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        client.force_login(user)
        return client

    def _message(self, account, wamid, *, sender="5511988887777", body="Tem estoque?", minutes_ago=5):
        return InboxMessage.objects.create(
            workspace=account.workspace,
            social_account=account,
            platform_message_id=wamid,
            message_type="dm",
            sender_name="Rita",
            sender_handle=sender,
            body=body,
            extra={"sender_id": sender},
            received_at=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def _sent(self, wamid="wamid.out"):
        response = MagicMock(status_code=200)
        response.json.return_value = {"messages": [{"id": wamid}]}
        return patch.object(WhatsAppProvider, "_request", return_value=response)

    def _url(self, name, message):
        return reverse(f"inbox:{name}", kwargs={"workspace_id": message.workspace_id, "message_id": message.id})

    def test_each_customer_bubble_offers_the_menu_and_shows_reactions(self, member_client, wa_account):
        message = self._message(wa_account, "wamid.in")
        message.extra = {**message.extra, "team_reaction": "🙏"}
        message.save(update_fields=["extra"])
        InboxReply.objects.create(
            inbox_message=message,
            body="Temos!",
            status=InboxReply.Status.SENT,
            sent_at=timezone.now(),
            extra={"customer_reaction": "❤️", "quote": {"sender_name": "Rita", "text": "Tem estoque?"}},
        )

        body = member_client.get(self._url("message_detail", message)).content.decode()

        assert 'x-data="inboxMessageMenu()"' in body
        assert self._url("react_message", message) in body
        assert f'id="reaction-{message.id}"' in body and "🙏" in body
        assert "❤️" in body and "chat-quote" in body

    def test_the_open_conversation_draws_what_arrived_and_nothing_otherwise(self, member_client, wa_account):
        opened = self._message(wa_account, "wamid.open", body="Primeira", minutes_ago=10)
        panel = member_client.get(self._url("message_detail", opened)).content.decode()
        poll = panel.split('hx-get="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
        assert poll.startswith(self._url("message_thread", opened))

        assert member_client.get(poll).status_code == 204

        later = self._message(wa_account, "wamid.later", body="Chegou depois", minutes_ago=1)
        self._message(wa_account, "wamid.other", sender="5511900000000", body="De outra pessoa", minutes_ago=1)
        InboxReply.objects.create(
            inbox_message=later, body="Resposta de um colega", status=InboxReply.Status.SENT, sent_at=timezone.now()
        )
        drawn = member_client.get(poll)

        body = drawn.content.decode()
        assert drawn.status_code == 200
        assert "Primeira" in body and "Chegou depois" in body and "Resposta de um colega" in body
        assert "De outra pessoa" not in body
        again = body.split('hx-get="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
        assert member_client.get(again).status_code == 204

    def test_a_conversation_of_another_workspace_cannot_be_polled(self, member_client, wa_account, organization):
        from apps.workspaces.models import Workspace

        elsewhere = SocialAccount.objects.create(
            workspace=Workspace.objects.create(name="Other", organization=organization),
            platform="whatsapp",
            account_platform_id="3003",
            webhook_target_id="4004",
            account_name="Outra",
            oauth_access_token="t",
        )
        foreign = self._message(elsewhere, "wamid.elsewhere")
        url = reverse(
            "inbox:message_thread", kwargs={"workspace_id": wa_account.workspace_id, "message_id": foreign.id}
        )

        assert member_client.get(url).status_code == 404

    def test_a_reply_quotes_the_message_the_person_picked(self, member_client, wa_account):
        first = self._message(wa_account, "wamid.first", body="Qual o prazo?", minutes_ago=10)
        latest = self._message(wa_account, "wamid.latest")

        with self._sent() as request:
            response = member_client.post(self._url("send_reply", latest), {"body": "Dois dias", "quote": first.id})

        assert response.status_code == 200
        assert request.call_args.kwargs["json"]["context"] == {"message_id": "wamid.first"}
        reply = InboxReply.objects.get()
        assert reply.inbox_message == latest
        assert reply.extra["quote"] == {
            "message_id": str(first.id),
            "platform_id": "wamid.first",
            "sender_name": "Rita",
            "text": "Qual o prazo?",
        }
        assert b"Qual o prazo?" in response.content

    @pytest.mark.parametrize("quote", ["not-a-uuid", "other-person", "other-workspace"])
    def test_a_message_from_outside_the_conversation_cannot_be_quoted(
        self, member_client, wa_account, organization, quote
    ):
        from apps.workspaces.models import Workspace

        latest = self._message(wa_account, "wamid.latest")
        if quote == "other-person":
            quote = self._message(wa_account, "wamid.other", sender="5511900000000").id
        elif quote == "other-workspace":
            elsewhere = SocialAccount.objects.create(
                workspace=Workspace.objects.create(name="Other", organization=organization),
                platform="whatsapp",
                account_platform_id="3003",
                webhook_target_id="4004",
                account_name="Outra",
                oauth_access_token="t",
            )
            quote = self._message(elsewhere, "wamid.elsewhere").id

        with self._sent() as request:
            response = member_client.post(self._url("send_reply", latest), {"body": "Oi", "quote": quote})

        assert response.status_code == 400
        request.assert_not_called()
        assert not InboxReply.objects.exists()

    def test_a_reaction_is_sent_and_shown_on_the_message(self, member_client, wa_account):
        message = self._message(wa_account, "wamid.in")

        with self._sent() as request:
            response = member_client.post(self._url("react_message", message), {"emoji": "👍"})

        payload = request.call_args.kwargs["json"]
        assert (payload["type"], payload["to"]) == ("reaction", "5511988887777")
        assert payload["reaction"] == {"message_id": "wamid.in", "emoji": "👍"}
        message.refresh_from_db()
        assert message.extra["team_reaction"] == "👍"
        assert "👍" in response.content.decode()

        with self._sent():
            member_client.post(self._url("react_message", message), {"emoji": ""})
        message.refresh_from_db()
        assert "team_reaction" not in message.extra

    def test_a_reaction_the_platform_refuses_is_not_recorded(self, member_client, wa_account):
        message = self._message(wa_account, "wamid.in")

        with patch.object(WhatsAppProvider, "_request", side_effect=MessagingError("Outside the window")):
            response = member_client.post(self._url("react_message", message), {"emoji": "👍"})

        assert response.status_code == 204
        assert json.loads(response["HX-Trigger"])["showToast"]["tone"] == "error"
        message.refresh_from_db()
        assert "team_reaction" not in message.extra

    def test_only_the_offered_reactions_and_only_in_the_reply_window(self, member_client, wa_account):
        message = self._message(wa_account, "wamid.in")
        expired = self._message(wa_account, "wamid.old", sender="5511900000000", minutes_ago=60 * 30)

        with self._sent() as request:
            member_client.post(self._url("react_message", message), {"emoji": "<script>"})
            member_client.post(self._url("react_message", expired), {"emoji": "👍"})

        request.assert_not_called()

    def test_a_message_of_another_workspace_cannot_be_reacted_to(self, member_client, wa_account, organization):
        from apps.workspaces.models import Workspace

        elsewhere = SocialAccount.objects.create(
            workspace=Workspace.objects.create(name="Other", organization=organization),
            platform="whatsapp",
            account_platform_id="3003",
            webhook_target_id="4004",
            account_name="Outra",
            oauth_access_token="t",
        )
        foreign = self._message(elsewhere, "wamid.elsewhere")
        url = reverse("inbox:react_message", kwargs={"workspace_id": wa_account.workspace_id, "message_id": foreign.id})

        with self._sent() as request:
            assert member_client.post(url, {"emoji": "👍"}).status_code == 404
        request.assert_not_called()

    def test_the_customer_reaction_lands_on_our_reply_and_can_be_taken_off(self, client, wa_account):
        message = self._message(wa_account, "wamid.in")
        reply = InboxReply.objects.create(
            inbox_message=message, body="Temos!", status=InboxReply.Status.SENT, platform_reply_id="wamid.out"
        )
        reaction = {"from": "5511988887777", "id": "wamid.r1", "timestamp": "1", "type": "reaction"}

        _post(client, _payload(messages=[{**reaction, "reaction": {"message_id": "wamid.out", "emoji": "❤️"}}]))
        reply.refresh_from_db()
        assert reply.extra["customer_reaction"] == "❤️"
        assert InboxMessage.objects.count() == 1

        _post(client, _payload(messages=[{**reaction, "id": "wamid.r2", "reaction": {"message_id": "wamid.out"}}]))
        reply.refresh_from_db()
        assert "customer_reaction" not in reply.extra
