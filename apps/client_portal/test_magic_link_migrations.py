"""The token-hashing migrations over links that were already sent.

Links e-mailed before the change sit in the database in plaintext. After the
migrations they must be stored only as their hash and still log the client in;
rolling back must not die on the now-empty plaintext column.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.client_portal.models import MagicLinkToken, hash_magic_token
from apps.client_portal.services import consume_magic_link, peek_magic_link
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

APP = "client_portal"
INITIAL = "0001_initial"


def _head() -> str:
    return MigrationExecutor(connection).loader.graph.leaf_nodes(APP)[0][1]


def _migrate(target: str) -> None:
    MigrationExecutor(connection).migrate([(APP, target)])


def _token_model_at(target: str):
    return MigrationExecutor(connection).loader.project_state((APP, target)).apps.get_model(APP, "MagicLinkToken")


@pytest.fixture
def restore_migrations():
    """No other app's migrations depend on this one, so re-applying it is enough."""
    yield
    _migrate(_head())


@pytest.fixture
def client_member(user):
    org = Organization.objects.create(name="Agency")
    workspace = Workspace.objects.create(organization=org, name="Brand")
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.CLIENT
    )
    return user, workspace


@pytest.mark.django_db(transaction=True)
def test_links_sent_before_the_change_are_hashed_and_keep_working(client_member, restore_migrations):
    user, workspace = client_member
    _migrate(INITIAL)
    old_token_model = _token_model_at(INITIAL)
    live = secrets.token_urlsafe(48)
    expired = secrets.token_urlsafe(48)
    old_token_model.objects.create(user_id=user.pk, workspace_id=workspace.pk, token=live)
    old_token_model.objects.create(
        user_id=user.pk,
        workspace_id=workspace.pk,
        token=expired,
        expires_at=timezone.now() - timedelta(days=1),
    )

    _migrate(_head())

    assert MagicLinkToken.objects.count() == 2
    assert not MagicLinkToken.objects.filter(token__isnull=False).exists()
    assert set(MagicLinkToken.objects.values_list("token_hash", flat=True)) == {
        hash_magic_token(live),
        hash_magic_token(expired),
    }
    assert peek_magic_link(live) is not None
    assert peek_magic_link(expired) is None
    assert consume_magic_link(live) == (user, workspace, True)


@pytest.mark.django_db(transaction=True)
def test_the_migrations_roll_back_with_hashed_links_present(client_member, restore_migrations):
    user, workspace = client_member
    _migrate(_head())
    for _ in range(2):
        MagicLinkToken.objects.create(
            user=user, workspace=workspace, token_hash=hash_magic_token(secrets.token_urlsafe(48))
        )

    _migrate(INITIAL)

    tokens = list(_token_model_at(INITIAL).objects.values_list("token", flat=True))
    assert len(tokens) == 2
    assert all(tokens) and len(set(tokens)) == 2
