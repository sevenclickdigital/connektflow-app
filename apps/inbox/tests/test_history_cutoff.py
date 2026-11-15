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


def _clear_url(account):
    return reverse(
        "social_accounts:clear_inbox_history",
        kwargs={"workspace_id": account.workspace_id, "account_id": account.id},
    )


def _card(client, account, **params):
    url = reverse(
        "social_accounts:account_card", kwargs={"workspace_id": account.workspace_id, "account_id": account.id}
    )
    return client.get(url, params)


def _history(provider_messages):
    """Patch the provider the walk reads from."""
    patcher = patch("apps.inbox.tasks.get_provider")
    get_provider = patcher.start()
    get_provider.return_value.get_messages.return_value = provider_messages
    return patcher


def test_import_history_marks_the_job_running_and_queues_the_walk(manager, account):
    with patch("apps.inbox.tasks.import_inbox_history") as task:
        response = manager.post(_import_url(account))

    assert response.status_code == 302
    account.refresh_from_db()
    assert account.inbox_history_job_state == "running"
    # The floor stays until the walk succeeds, so a failure changes nothing.
    assert account.inbox_history_cutoff is not None
    task.assert_called_once_with(str(account.id))


def test_a_running_job_cannot_be_started_again(manager, account):
    with patch("apps.inbox.tasks.import_inbox_history") as task:
        manager.post(_import_url(account))
        manager.post(_import_url(account))

    assert task.call_count == 1


@pytest.mark.parametrize("url_name", ["import_inbox_history", "clear_inbox_history", "account_card"])
def test_the_history_actions_are_refused_for_another_workspaces_account(manager, account, organization, url_name):
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(name="Other WS", organization=organization)
    url = reverse(f"social_accounts:{url_name}", kwargs={"workspace_id": other.id, "account_id": account.id})

    with (
        patch("apps.inbox.tasks.import_inbox_history") as importing,
        patch("apps.inbox.tasks.clear_inbox_history") as clearing,
    ):
        response = manager.get(url) if url_name == "account_card" else manager.post(url)

    assert response.status_code in (403, 404)
    importing.assert_not_called()
    clearing.assert_not_called()
    account.refresh_from_db()
    assert account.inbox_history_job is None


def test_import_history_waits_for_a_healthy_connection(manager, account):
    account.connection_status = SocialAccount.ConnectionStatus.ERROR
    account.save()

    with patch("apps.inbox.tasks.import_inbox_history") as task:
        manager.post(_import_url(account))

    task.assert_not_called()
    account.refresh_from_db()
    assert account.inbox_history_job is None


def test_the_walk_brings_in_what_the_polls_had_dropped_and_lifts_the_floor(account):
    _poll(account, [_msg("old", hours_ago=30), _msg("new", hours_ago=0)])
    assert _stored(account) == {"new"}
    account.set_inbox_history_job("import", "running")

    patcher = _history([_msg("old", hours_ago=30), _msg("new", hours_ago=0)])
    try:
        with patch.object(InboxSyncEngine, "_notify_new_message") as notify:
            import_inbox_history.now(str(account.id))
    finally:
        patcher.stop()

    assert _stored(account) == {"old", "new"}
    notify.assert_not_called()
    account.refresh_from_db()
    assert account.inbox_history_cutoff is None
    assert account.inbox_history_job["state"] == "done"
    assert account.inbox_history_job["count"] == 2


def test_a_failed_walk_leaves_the_floor_and_says_why(account):
    from providers.exceptions import APIError

    floor = account.inbox_history_cutoff
    account.set_inbox_history_job("import", "running")

    with patch("apps.inbox.tasks.get_provider") as get_provider:
        get_provider.return_value.get_messages.side_effect = APIError("boom", platform="facebook")
        import_inbox_history.now(str(account.id))

    account.refresh_from_db()
    assert account.inbox_history_cutoff == floor
    assert account.inbox_history_job["state"] == "failed"
    assert account.inbox_history_job["error"]
    # Offered again, so the person can retry.
    assert account.can_import_inbox_history


# ------------------------------------------------------------ removing history


@pytest.fixture
def account_with_history(inbox_account):
    """Connected a day ago, holding one message from before that and one from after."""
    inbox_account.connection_status = SocialAccount.ConnectionStatus.CONNECTED
    inbox_account.save()
    SocialAccount.objects.filter(pk=inbox_account.pk).update(connected_at=timezone.now() - timedelta(days=1))
    inbox_account.refresh_from_db()
    for message_id, hours_ago in (("before", 72), ("after", 2)):
        InboxMessage.objects.create(
            workspace=inbox_account.workspace,
            social_account=inbox_account,
            platform_message_id=message_id,
            message_type=InboxMessage.MessageType.COMMENT,
            sender_name="Ada",
            body="hi",
            received_at=timezone.now() - timedelta(hours=hours_ago),
        )
    return inbox_account


def test_an_account_without_a_floor_is_offered_removal_not_import(account_with_history):
    assert account_with_history.can_clear_inbox_history
    assert not account_with_history.can_import_inbox_history


def test_an_account_with_a_floor_is_offered_import_not_removal(account):
    assert account.can_import_inbox_history
    assert not account.can_clear_inbox_history


def test_remove_old_history_marks_the_job_running_and_queues_it(manager, account_with_history):
    with patch("apps.inbox.tasks.clear_inbox_history") as task:
        response = manager.post(_clear_url(account_with_history))

    assert response.status_code == 302
    task.assert_called_once_with(str(account_with_history.id))
    account_with_history.refresh_from_db()
    assert account_with_history.inbox_history_job_state == "running"
    assert _stored(account_with_history) == {"before", "after"}


def test_removing_keeps_what_came_after_the_connection_and_puts_the_floor_back(account_with_history):
    from apps.inbox.models import InboxReply
    from apps.inbox.tasks import clear_inbox_history

    old = InboxMessage.objects.get(social_account=account_with_history, platform_message_id="before")
    InboxReply.objects.create(inbox_message=old, body="thanks")
    someone_elses = SocialAccount.objects.create(
        workspace=account_with_history.workspace, platform="instagram", account_platform_id="ig-1", account_name="IG"
    )
    untouched = InboxMessage.objects.create(
        workspace=someone_elses.workspace,
        social_account=someone_elses,
        platform_message_id="other-old",
        message_type=InboxMessage.MessageType.COMMENT,
        sender_name="Bo",
        body="hey",
        received_at=timezone.now() - timedelta(days=30),
    )

    clear_inbox_history.now(str(account_with_history.id))

    assert _stored(account_with_history) == {"after"}
    assert not InboxReply.objects.filter(inbox_message_id=old.id).exists()
    assert InboxMessage.objects.filter(pk=untouched.pk).exists()
    account_with_history.refresh_from_db()
    assert account_with_history.inbox_history_cutoff == account_with_history.connected_at
    assert account_with_history.inbox_history_job == {"action": "clear", "state": "done", "count": 1}
    # And a later poll no longer brings the old message back.
    _poll(account_with_history, [_msg("before", hours_ago=72), _msg("later", hours_ago=0)])
    assert _stored(account_with_history) == {"after", "later"}


# ------------------------------------------------------------------- the card


def test_the_card_polls_while_a_job_runs_and_announces_how_it_ended(manager, account):
    import json

    account.set_inbox_history_job("import", "running")
    running = _card(manager, account, was="running")
    assert "every 4s" in running.content.decode()
    assert "HX-Trigger" not in running

    account.set_inbox_history_job("import", "done", count=3)
    done = _card(manager, account, was="running")
    assert "every 4s" not in done.content.decode()
    toast = json.loads(done["HX-Trigger"])["showToast"]
    assert toast["tone"] == "success" and "3" in toast["title"]
    # Only the poll that was waiting announces it; opening the page later does not.
    assert "HX-Trigger" not in _card(manager, account)

    account.set_inbox_history_job("clear", "failed", error="")
    failed = json.loads(_card(manager, account, was="running")["HX-Trigger"])["showToast"]
    assert failed["tone"] == "error"


def test_a_job_whose_worker_died_stops_blocking_the_account(account):
    account.inbox_history_job = {
        "action": "import",
        "state": "running",
        "started_at": (timezone.now() - timedelta(hours=2)).isoformat(),
    }

    assert account.inbox_history_job_state == ""
    assert account.can_import_inbox_history


def test_the_card_offers_the_import_only_while_there_is_a_floor_and_an_inbox_to_read(account):
    assert account.can_import_inbox_history
    account.inbox_history_cutoff = None
    assert not account.can_import_inbox_history

    whatsapp = SocialAccount(platform="whatsapp", inbox_history_cutoff=timezone.now())
    assert not whatsapp.can_import_inbox_history
    assert polls_inbox("facebook") and polls_inbox("instagram") and polls_inbox("youtube")
    assert not polls_inbox("whatsapp") and not polls_inbox("pinterest")
