"""Account centre → Brands: the organization's brands plus those of other
organizations the user was added to — and never one they don't belong to."""

import pytest
from django.urls import reverse

from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


@pytest.fixture
def other_org(db):
    return Organization.objects.create(name="Other Agency")


@pytest.mark.django_db
def test_lists_own_brands_and_brands_the_user_was_added_to(client, org_owner, organization, other_org):
    Workspace.objects.create(organization=organization, name="Own Brand")
    shared = Workspace.objects.create(organization=other_org, name="Client Brand")
    WorkspaceMembership.objects.create(
        user=org_owner, workspace=shared, workspace_role=WorkspaceMembership.WorkspaceRole.MANAGER
    )
    client.force_login(org_owner)

    response = client.get(reverse("organizations:workspaces"))

    assert response.status_code == 200
    # The user is in two organizations (creating a user provisions one), so
    # whichever is current, both brands must be on the page.
    listed = [e["workspace"].name for e in response.context["workspace_data"]]
    external = {e["workspace"].name: e for e in response.context["external_data"]}
    assert "Own Brand" in listed + list(external)
    assert "Client Brand" in external
    assert external["Client Brand"]["organization"] == other_org
    assert external["Client Brand"]["can_manage"] is True


@pytest.mark.django_db
def test_never_lists_a_brand_of_another_organization_without_membership(client, org_owner, other_org):
    Workspace.objects.create(organization=other_org, name="Someone Else's Brand")
    client.force_login(org_owner)

    response = client.get(reverse("organizations:workspaces"))

    names = [e["workspace"].name for e in response.context["external_data"]]
    assert "Someone Else's Brand" not in names
    assert b"Someone Else&#x27;s Brand" not in response.content
    assert b"Someone Else's Brand" not in response.content


@pytest.mark.django_db
def test_other_members_of_an_outside_brand_are_not_shown(client, org_owner, other_org, django_user_model):
    shared = Workspace.objects.create(organization=other_org, name="Client Brand")
    WorkspaceMembership.objects.create(
        user=org_owner, workspace=shared, workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR
    )
    stranger = django_user_model.objects.create_user(email="stranger@example.com", password="x", name="Stranger Person")
    WorkspaceMembership.objects.create(
        user=stranger, workspace=shared, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    client.force_login(org_owner)

    response = client.get(reverse("organizations:workspaces"))

    assert b"Client Brand" in response.content
    assert b"Stranger Person" not in response.content
    external = {e["workspace"].name: e for e in response.context["external_data"]}
    assert external["Client Brand"]["can_manage"] is False
