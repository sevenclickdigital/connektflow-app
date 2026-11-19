"""The Threads webhook: a reply on a connected profile's post lands in the inbox at once."""

import hashlib
import hmac
import json

import pytest
from django.urls import reverse

from apps.inbox.models import InboxMessage
from apps.social_accounts.models import SocialAccount

SECRET = "threads-app-secret"
WEBHOOK = "inbox_webhooks:webhook_threads"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _threads_app(settings):
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {"threads": {"app_id": "th-app", "app_secret": SECRET}}
    settings.THREADS_WEBHOOK_VERIFY_TOKEN = "verify-me"


@pytest.fixture
def profile(organization):
    from apps.workspaces.models import Workspace

    return SocialAccount.objects.create(
        workspace=Workspace.objects.create(name="Threads WS", organization=organization),
        platform="threads",
        account_platform_id="owner-1",
        account_name="Loja",
        account_handle="loja",
        oauth_access_token="token",
    )


def _reply(**value):
    return {
        "app_id": "th-app",
        "topic": "moderate",
        "target_id": "post-1",
        "time": 1723226877,
        "values": {
            "field": "replies",
            "value": {
                "id": "reply-1",
                "username": "rita",
                "text": "Tem estoque?",
                "media_type": "TEXT_POST",
                "permalink": "https://www.threads.com/@rita/post/Pp",
                "replied_to": {"id": "post-1"},
                "root_post": {"id": "post-1", "owner_id": "owner-1", "username": "loja"},
                "timestamp": "2026-11-19T22:10:00+0000",
                **value,
            },
        },
    }


def _post(client, payload, secret=SECRET):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(reverse(WEBHOOK), data=body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=signature)


def test_the_handshake_echoes_the_challenge(client):
    response = client.get(
        reverse(WEBHOOK), {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"}
    )

    assert (response.status_code, response.content) == (200, b"42")


def test_a_delivery_signed_with_another_secret_is_refused(client, profile):
    assert _post(client, _reply(), secret="someone-else").status_code == 403
    assert not InboxMessage.objects.exists()


def test_a_reply_is_filed_as_a_comment_on_the_post_once(client, profile):
    assert _post(client, _reply()).status_code == 200
    _post(client, _reply())

    message = InboxMessage.objects.get()
    assert message.social_account == profile
    assert message.message_type == InboxMessage.MessageType.COMMENT
    assert (message.platform_message_id, message.sender_name, message.body) == ("reply-1", "rita", "Tem estoque?")
    assert message.extra["stored_post_id"] == "post-1"
    assert message.received_at.isoformat() == "2026-11-19T22:10:00+00:00"


def test_the_profile_s_own_reply_is_not_filed(client, profile):
    _post(client, _reply(username="loja"))

    assert not InboxMessage.objects.exists()


def test_a_reply_on_a_post_no_connected_profile_owns_is_logged(client, profile, caplog):
    stranger = {"id": "post-9", "owner_id": "owner-9", "username": "outra"}

    with caplog.at_level("WARNING", logger="apps.inbox.webhooks"):
        response = _post(client, _reply(root_post=stranger))

    assert response.status_code == 200
    assert not InboxMessage.objects.exists()
    assert "no connected profile owns post post-9" in caplog.text


def test_other_fields_are_ignored(client, profile):
    payload = _reply()
    payload["values"]["field"] = "publish"

    assert _post(client, payload).status_code == 200
    assert not InboxMessage.objects.exists()
