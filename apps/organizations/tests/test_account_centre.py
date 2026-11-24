"""The account centre after the General page left the menu: where the
organization's name, its deletion and the timezone are found now."""

import importlib

import pytest
from django.apps import apps as global_apps
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


def _owner(email="owner@a.com"):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    workspace = WorkspaceMembership.objects.get(user=user).workspace
    return user, workspace


@pytest.mark.django_db
def test_a_new_account_starts_on_brasilia_time():
    _user, workspace = _owner()
    assert workspace.effective_timezone == "America/Sao_Paulo"


@pytest.mark.django_db
def test_the_organization_name_is_changed_from_the_organizations_page(client):
    user, workspace = _owner()
    client.force_login(user)

    response = client.post(
        reverse("organizations:rename"), {"organization": workspace.organization_id, "name": "Agência Sol"}
    )

    assert response.headers["Location"] == reverse("organizations:workspaces")
    workspace.organization.refresh_from_db()
    assert workspace.organization.name == "Agência Sol"


@pytest.mark.django_db
def test_a_brand_manager_sets_the_brand_timezone(client):
    user, workspace = _owner()
    client.force_login(user)
    url = reverse("workspaces:settings", kwargs={"workspace_id": workspace.id})

    client.post(url, {"action": "update_general", "timezone": "America/Manaus"})
    workspace.refresh_from_db()
    assert workspace.timezone == "America/Manaus"

    client.post(url, {"action": "update_general", "timezone": "Mars/Olympus"})
    workspace.refresh_from_db()
    assert workspace.timezone == "America/Manaus"


@pytest.mark.django_db
def test_a_viewer_cannot_set_the_brand_timezone(client):
    _user, workspace = _owner()
    viewer = User.objects.create_user(email="viewer@b.com", password="x", tos_accepted_at=timezone.now())
    WorkspaceMembership.objects.create(
        user=viewer, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
    )
    client.force_login(viewer)

    client.post(
        reverse("workspaces:settings", kwargs={"workspace_id": workspace.id}),
        {"action": "update_general", "timezone": "America/Manaus"},
    )

    workspace.refresh_from_db()
    assert workspace.timezone == ""


@pytest.mark.django_db
def test_only_an_admin_may_edit_the_organization_on_its_page(client):
    user, _workspace = _owner()
    client.force_login(user)
    assert client.get(reverse("organizations:workspaces")).context["org_groups"][0]["can_edit"] is True

    OrgMembership.objects.filter(user=user).update(org_role=OrgMembership.OrgRole.MEMBER)
    assert client.get(reverse("organizations:workspaces")).context["org_groups"][0]["can_edit"] is False


@pytest.mark.django_db
def test_the_migration_moves_only_the_untouched_default():
    migration = importlib.import_module("apps.organizations.migrations.0003_default_timezone_brasilia")
    untouched = Organization.objects.create(name="Old default", default_timezone="UTC")
    chosen = Organization.objects.create(name="Chose Lisbon", default_timezone="Europe/Lisbon")
    own = Workspace.objects.create(organization=untouched, name="Own timezone", timezone="America/Manaus")

    migration.utc_to_brasilia(global_apps, None)

    untouched.refresh_from_db()
    chosen.refresh_from_db()
    own.refresh_from_db()
    assert untouched.default_timezone == "America/Sao_Paulo"
    assert chosen.default_timezone == "Europe/Lisbon"
    assert own.timezone == "America/Manaus"
