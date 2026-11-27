"""The account menu's "Import data…": what each account offers, who may use it,
and that only what was ticked and offered is imported.

Asserts on status, redirects and what was called. No assertion on rendered
sentences: the project defaults to pt-BR and copy is not a contract.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.analytics.history_import import ImportResult
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


def _member(workspace, email, role):
    user = User.objects.create_user(email=email, password="pw", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(
        user=user,
        organization=workspace.organization,
        org_role=OrgMembership.OrgRole.OWNER if role == "owner" else OrgMembership.OrgRole.MEMBER,
    )
    WorkspaceMembership.objects.create(
        user=user,
        workspace=workspace,
        workspace_role=(
            WorkspaceMembership.WorkspaceRole.OWNER if role == "owner" else WorkspaceMembership.WorkspaceRole.VIEWER
        ),
    )
    return user


def _account(workspace, platform, **extra):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-1",
        account_name=platform,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        **extra,
    )


@pytest.fixture
def setup(db, client):
    workspace = Workspace.objects.create(organization=Organization.objects.create(name="Org"), name="WS")
    client.force_login(_member(workspace, "owner@example.com", "owner"))
    return {"workspace": workspace, "client": client}


def _url(s, account):
    return reverse(
        "social_accounts:import_account_data", kwargs={"workspace_id": s["workspace"].id, "account_id": account.id}
    )


def test_each_platform_offers_only_what_it_can_give(setup):
    ws = setup["workspace"]
    assert _account(ws, "pinterest").import_options == {"posts": "ready"}
    assert set(_account(ws, "threads").import_options) >= {"posts", "series"}
    assert _account(ws, "bluesky").import_options.get("posts") is None


def test_the_menu_opens_the_modal_only_where_there_is_something_to_import(setup):
    pinterest = _account(setup["workspace"], "pinterest")
    body = setup["client"].get(reverse("social_accounts:list", args=[setup["workspace"].id])).content.decode()
    assert _url(setup, pinterest) in body


def test_only_what_was_ticked_and_offered_is_imported(setup):
    pinterest = _account(setup["workspace"], "pinterest")
    with (
        patch("apps.analytics.history_import.import_history", return_value=ImportResult(imported=2)) as posts,
        patch("apps.analytics.tasks.backfill_account_series") as series,
        patch("apps.inbox.tasks.import_inbox_history") as inbox,
    ):
        response = setup["client"].post(_url(setup, pinterest), {"kinds": ["posts", "series", "inbox"]})
    assert response.status_code == 302
    assert response["Location"] == reverse("social_accounts:list", args=[setup["workspace"].id])
    posts.assert_called_once()
    series.assert_not_called()  # Pinterest gives no daily series: not offered, so not run.
    inbox.assert_not_called()


def test_nothing_ticked_imports_nothing(setup):
    pinterest = _account(setup["workspace"], "pinterest")
    with patch("apps.analytics.history_import.import_history") as posts:
        setup["client"].post(_url(setup, pinterest), {})
    posts.assert_not_called()


def test_a_viewer_may_not_import(setup, client):
    pinterest = _account(setup["workspace"], "pinterest")
    client.force_login(_member(setup["workspace"], "viewer@example.com", "viewer"))
    with patch("apps.analytics.history_import.import_history") as posts:
        response = client.post(_url(setup, pinterest), {"kinds": ["posts"]})
    assert response.status_code == 403
    posts.assert_not_called()


def test_another_workspaces_account_is_not_found(setup):
    other = Workspace.objects.create(organization=setup["workspace"].organization, name="Other")
    foreign = _account(other, "pinterest")
    url = reverse(
        "social_accounts:import_account_data", kwargs={"workspace_id": setup["workspace"].id, "account_id": foreign.id}
    )
    with patch("apps.analytics.history_import.import_history") as posts:
        response = setup["client"].post(url, {"kinds": ["posts"]})
    assert response.status_code == 404
    posts.assert_not_called()


def test_the_period_reaches_every_import(setup):
    threads = _account(setup["workspace"], "threads")
    with (
        patch("apps.analytics.history_import.import_history", return_value=ImportResult()) as posts,
        patch("apps.analytics.tasks.backfill_account_series", return_value=0) as series,
    ):
        setup["client"].post(_url(setup, threads), {"kinds": ["posts", "series"], "period": "30"})
    since = posts.call_args.kwargs["since"]
    assert timezone.now() - since > timedelta(days=29)
    assert series.call_args.kwargs["days"] == 30


def test_everything_the_network_gives_sends_no_start_date(setup):
    threads = _account(setup["workspace"], "threads")
    with (
        patch("apps.analytics.history_import.import_history", return_value=ImportResult()) as posts,
        patch("apps.analytics.tasks.backfill_account_series", return_value=0) as series,
    ):
        setup["client"].post(_url(setup, threads), {"kinds": ["posts", "series"], "period": "all"})
    assert posts.call_args.kwargs["since"] is None
    assert series.call_args.kwargs["days"] is None
