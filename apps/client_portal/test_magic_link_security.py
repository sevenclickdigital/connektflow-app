"""Magic links are stored hashed, hidden from the admin, and open the portal only.

A magic link is a password sent by e-mail. Stored in plaintext it was readable
by anyone with database or admin access; redeemed, it opened a full session on
the whole account, not just the portal of the workspace it was sent for.
"""

import re

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.client_portal.models import MagicLinkToken, hash_magic_token
from apps.client_portal.services import generate_magic_link
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


@pytest.fixture
def portal(db):
    org = Organization.objects.create(name="Agency")
    workspace = Workspace.objects.create(organization=org, name="Brand")
    manager = User.objects.create_user(email="manager@example.com", password="x", tos_accepted_at=timezone.now())
    client_user = User.objects.create_user(
        email="client@example.com", password="client-pass-123", tos_accepted_at=timezone.now()
    )
    WorkspaceMembership.objects.create(
        user=client_user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.CLIENT
    )
    # The client also owns an organization of their own: the session a link
    # opens must not reach it.
    own_org = Organization.objects.create(name="Client's own")
    OrgMembership.objects.create(user=client_user, organization=own_org, org_role=OrgMembership.OrgRole.OWNER)
    own_workspace = Workspace.objects.create(organization=own_org, name="Own brand")
    WorkspaceMembership.objects.create(
        user=client_user, workspace=own_workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return {
        "workspace": workspace,
        "manager": manager,
        "client_user": client_user,
        "own_org": own_org,
        "own_workspace": own_workspace,
    }


def _send_link(portal):
    """Send a link and return ``(token_row, raw_token)`` as the client receives it."""
    token = generate_magic_link(
        workspace=portal["workspace"], client_user=portal["client_user"], created_by=portal["manager"]
    )
    body = mail.outbox[-1].body
    raw = re.search(r"/portal/([A-Za-z0-9_-]+)/", body).group(1)
    return token, raw


def _redeem(client, raw):
    return client.post(reverse("client_portal:magic_link_entry", kwargs={"token": raw}))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_the_hash_of_the_token_is_stored(portal):
    token, raw = _send_link(portal)

    token.refresh_from_db()
    assert token.token is None
    assert token.token_hash == hash_magic_token(raw)
    assert raw not in token.token_hash
    assert not MagicLinkToken.objects.filter(token=raw).exists()


@pytest.mark.django_db
def test_the_e_mailed_link_still_logs_the_client_in(client, portal):
    token, raw = _send_link(portal)

    response = _redeem(client, raw)

    assert response.status_code == 302
    assert response.url == reverse("client_portal:dashboard")
    token.refresh_from_db()
    assert token.is_consumed
    assert client.get(reverse("client_portal:dashboard")).status_code == 200


@pytest.mark.django_db
def test_the_stored_hash_does_not_work_as_a_link(client, portal):
    token, _raw = _send_link(portal)

    response = _redeem(client, token.token_hash)

    assert response.url == reverse("client_portal:magic_link_expired")
    token.refresh_from_db()
    assert not token.is_consumed


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_admin_shows_neither_the_token_nor_its_hash(client, portal):
    token, raw = _send_link(portal)
    token.refresh_from_db()
    superuser = User.objects.create_superuser(email="root@example.com", password="x")
    client.force_login(superuser)

    for url in (
        reverse("admin:client_portal_magiclinktoken_changelist"),
        reverse("admin:client_portal_magiclinktoken_change", args=[token.pk]),
    ):
        content = client.get(url).content.decode()
        assert raw not in content
        assert token.token_hash not in content


# ---------------------------------------------------------------------------
# Confinement
# ---------------------------------------------------------------------------


@pytest.fixture
def portal_client(client, portal):
    _token, raw = _send_link(portal)
    _redeem(client, raw)
    assert client.session.get("is_portal_session")
    return client


@pytest.mark.django_db
def test_a_portal_session_is_sent_back_to_the_portal_from_a_workspace(portal_client, portal):
    for workspace in (portal["workspace"], portal["own_workspace"]):
        url = reverse("composer:create_landing", kwargs={"workspace_id": workspace.id})
        response = portal_client.get(url)
        assert response.status_code == 302
        assert response.url == reverse("client_portal:dashboard")


@pytest.mark.django_db
def test_a_portal_session_cannot_open_another_organization(portal_client, portal):
    for url in (
        reverse("organizations:manage", kwargs={"organization_id": portal["own_org"].id}),
        reverse("organizations:settings"),
        "/",
    ):
        response = portal_client.get(url)
        assert response.status_code == 302
        assert response.url == reverse("client_portal:dashboard")


@pytest.mark.django_db
def test_a_portal_session_is_refused_writes_and_htmx_outside_the_portal(portal_client, portal):
    url = reverse("composer:create_landing", kwargs={"workspace_id": portal["own_workspace"].id})

    assert portal_client.post(url).status_code == 403
    assert portal_client.get(url, HTTP_HX_REQUEST="true").status_code == 403


@pytest.mark.django_db
def test_a_portal_session_keeps_the_portal_language_switch_and_logout(portal_client):
    assert portal_client.get(reverse("client_portal:approval_queue")).status_code == 200

    response = portal_client.post("/i18n/setlang/", {"language": "en-us", "next": "/portal/"})
    assert response.status_code == 302
    assert response.url == "/portal/"

    response = portal_client.get(reverse("accounts:logout"))
    assert response.status_code == 302
    assert "_auth_user_id" not in portal_client.session


@pytest.mark.django_db
def test_signing_in_normally_ends_the_confinement(portal_client, portal):
    """``login()`` keeps the session data for the same user, flags included."""
    portal_client.login(email="client@example.com", password="client-pass-123")

    assert not portal_client.session.get("is_portal_session")
    assert not portal_client.session.get("portal_workspace_id")
    url = reverse("composer:create_landing", kwargs={"workspace_id": portal["own_workspace"].id})
    response = portal_client.get(url)
    assert not (response.status_code == 302 and response.url == reverse("client_portal:dashboard"))


@pytest.mark.django_db
def test_a_normal_session_is_not_confined(client, portal):
    client.login(email="client@example.com", password="client-pass-123")

    url = reverse("composer:create_landing", kwargs={"workspace_id": portal["own_workspace"].id})
    response = client.get(url)

    assert not (response.status_code == 302 and response.url == reverse("client_portal:dashboard"))
