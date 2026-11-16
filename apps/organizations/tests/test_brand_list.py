"""Account centre → Organizations: each organization with its brands, plus
those of organizations the user was only added to a brand of — and never one
they don't belong to."""

import pytest
from django.urls import reverse

from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


def _brands(response):
    """Every brand entry on the page by name, with the group it sits in."""
    return {
        entry["workspace"].name: (group, entry) for group in response.context["org_groups"] for entry in group["brands"]
    }


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
    brands = _brands(response)
    assert brands["Own Brand"][0]["organization"] == organization
    group, entry = brands["Client Brand"]
    assert group["organization"] == other_org
    # A guest of the brand, not of the organization: nothing to rename.
    assert group["can_edit"] is False
    assert entry["can_manage"] is True


@pytest.mark.django_db
def test_never_lists_a_brand_of_another_organization_without_membership(client, org_owner, other_org):
    Workspace.objects.create(organization=other_org, name="Someone Else's Brand")
    client.force_login(org_owner)

    response = client.get(reverse("organizations:workspaces"))

    assert "Someone Else's Brand" not in _brands(response)
    assert other_org not in [g["organization"] for g in response.context["org_groups"]]
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
    assert _brands(response)["Client Brand"][1]["can_manage"] is False
