"""A new connection starts with an empty inbox; its history comes only on request.

The floor (``SocialAccount.inbox_history_cutoff``) is set when an account is
first connected. Every poll drops what is older, an account without a floor
behaves as before, and "Import history" lifts it and goes back for the rest.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import InboxMessage
from apps.inbox.tasks import InboxSyncEngine, import_inbox_history
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.views import _create_or_update_account
from providers import polls_inbox
from providers.types import AccountProfile

pytestmark = pytest.mark.django_db


def _msg(message_id, *, hours_ago):
    return SimpleNamespace(
        platform_message_id=message_id,
        sender_name="Sender",
        sender_id="sender-1",
        text="hello",
        message_type=InboxMessage.MessageType.COMMENT,
        timestamp=timezone.now() - timedelta(hours=hours_ago),
        extra={},
    )


def _stored(account):
    return set(InboxMessage.objects.filter(social_account=account).values_list("platform_message_id", flat=True))


def _poll(account, messages):
    with patch("apps.inbox.tasks.get_provider") as get_provider:
        provider = get_provider.return_value
        provider.get_messages.return_value = messages
        with patch.object(InboxSyncEngine, "_notify_new_message"):
            InboxSyncEngine()._sync_account(account)
        return provider


@pytest.fixture
def account(inbox_account):
    inbox_account.connection_status = SocialAccount.ConnectionStatus.CONNECTED
    inbox_account.inbox_history_cutoff = timezone.now() - timedelta(hours=1)
    inbox_account.save()
    return inbox_account


# ------------------------------------------------------------------ connecting


def test_a_first_connection_starts_with_a_floor_and_reconnecting_does_not_move_it(inbox_workspace):
    profile = AccountProfile(platform_id="page-9", name="Page")
    with patch("apps.social_accounts.views.subscribe_account_webhooks_task"):
        before = timezone.now()
        created = _create_or_update_account(
            workspace_id=inbox_workspace.id, platform="facebook", profile=profile, access_token="t"
        )
        floor = created.inbox_history_cutoff
        assert before <= floor <= timezone.now()

        again = _create_or_update_account(
            workspace_id=inbox_workspace.id, platform="facebook", profile=profile, access_token="t2"
        )
    assert again.pk == created.pk
    assert again.inbox_history_cutoff == floor


def test_reconnecting_an_account_whose_history_was_imported_keeps_it_open(inbox_workspace, inbox_account):
    assert inbox_account.inbox_history_cutoff is None
    profile = AccountProfile(platform_id=inbox_account.account_platform_id, name="Page")
    with patch("apps.social_accounts.views.subscribe_account_webhooks_task"):
        again = _create_or_update_account(
            workspace_id=inbox_workspace.id, platform="facebook", profile=profile, access_token="t2"
        )
    assert again.pk == inbox_account.pk
    assert again.inbox_history_cutoff is None


# --------------------------------------------------------------------- polling


def test_a_poll_keeps_what_came_after_the_floor_and_drops_what_came_before(account):
    provider = _poll(account, [_msg("old", hours_ago=30), _msg("new", hours_ago=0)])

    assert _stored(account) == {"new"}
    # Nothing stored yet, so the floor is as far back as the poll asks.
    assert provider.get_messages.call_args.kwargs["since"] == account.inbox_history_cutoff


def test_an_account_without_a_floor_takes_everything_as_before(inbox_account):
    inbox_account.connection_status = SocialAccount.ConnectionStatus.CONNECTED
    inbox_account.save()

    provider = _poll(inbox_account, [_msg("old", hours_ago=30), _msg("new", hours_ago=0)])

    assert _stored(inbox_account) == {"old", "new"}
    assert provider.get_messages.call_args.kwargs["since"] is None


# ------------------------------------------------------------ importing history


@pytest.fixture
def manager(client, user, organization, inbox_workspace):
    from apps.members.models import OrgMembership, WorkspaceMembership

    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=inbox_workspace, workspace_role="manager")
    client.force_login(user)
    return client


def _import_url(account):
    return reverse(
        "social_accounts:import_inbox_history",
        kwargs={"workspace_id": account.workspace_id, "account_id": account.id},
    )


def test_import_history_lifts_the_floor_and_queues_the_walk(manager, account):
    with patch("apps.inbox.tasks.import_inbox_history") as task:
        response = manager.post(_import_url(account))

    assert response.status_code == 302
    account.refresh_from_db()
    assert account.inbox_history_cutoff is None
    task.assert_called_once_with(str(account.id))


def test_import_history_does_nothing_twice(manager, account):
    with patch("apps.inbox.tasks.import_inbox_history") as task:
        manager.post(_import_url(account))
        manager.post(_import_url(account))

    assert task.call_count == 1


def test_import_history_is_refused_for_another_workspaces_account(manager, account, organization):
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(name="Other WS", organization=organization)
    url = reverse("social_accounts:import_inbox_history", kwargs={"workspace_id": other.id, "account_id": account.id})

    with patch("apps.inbox.tasks.import_inbox_history") as task:
        response = manager.post(url)

    assert response.status_code in (403, 404)
    task.assert_not_called()
    account.refresh_from_db()
    assert account.inbox_history_cutoff is not None


def test_import_history_waits_for_a_healthy_connection(manager, account):
    account.connection_status = SocialAccount.ConnectionStatus.ERROR
    account.save()

    with patch("apps.inbox.tasks.import_inbox_history") as task:
        manager.post(_import_url(account))

    task.assert_not_called()
    account.refresh_from_db()
    assert account.inbox_history_cutoff is not None


def test_the_walk_brings_in_what_the_polls_had_dropped(account):
    _poll(account, [_msg("old", hours_ago=30), _msg("new", hours_ago=0)])
    assert _stored(account) == {"new"}
    SocialAccount.objects.filter(pk=account.pk).update(inbox_history_cutoff=None)

    with patch("apps.inbox.tasks.get_provider") as get_provider:
        get_provider.return_value.get_messages.return_value = [_msg("old", hours_ago=30), _msg("new", hours_ago=0)]
        with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
            import_inbox_history.now(str(account.id))

    assert _stored(account) == {"old", "new"}
    notify.assert_not_called()


def test_the_card_offers_the_import_only_while_there_is_a_floor_and_an_inbox_to_read(account):
    assert account.can_import_inbox_history
    account.inbox_history_cutoff = None
    assert not account.can_import_inbox_history

    whatsapp = SocialAccount(platform="whatsapp", inbox_history_cutoff=timezone.now())
    assert not whatsapp.can_import_inbox_history
    assert polls_inbox("facebook") and polls_inbox("instagram") and polls_inbox("youtube")
    assert not polls_inbox("whatsapp") and not polls_inbox("pinterest")
