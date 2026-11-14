"""Account centre → Billing: the brands whose billing the user manages, only."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

ROLE = WorkspaceMembership.WorkspaceRole


def _names(response):
    return {row["workspace"].name for row in response.context["rows"]}


@pytest.mark.django_db
def test_lists_the_brands_the_user_pays_for_and_nothing_else(client):
    user = User.objects.create_user(email="owner@a.com", password="x", tos_accepted_at=timezone.now())
    own_org = Organization.objects.create(name="Mine")
    OrgMembership.objects.create(user=user, organization=own_org, org_role=OrgMembership.OrgRole.OWNER)
    Workspace.objects.create(organization=own_org, name="Own brand")  # org owner, not a brand member

    other_org = Organization.objects.create(name="Theirs")
    managed = Workspace.objects.create(organization=other_org, name="Managed brand")
    WorkspaceMembership.objects.create(user=user, workspace=managed, workspace_role=ROLE.MANAGER)
    edited = Workspace.objects.create(organization=other_org, name="Edited brand")
    WorkspaceMembership.objects.create(user=user, workspace=edited, workspace_role=ROLE.EDITOR)
    Workspace.objects.create(organization=other_org, name="Unrelated brand")
    client.force_login(user)

    response = client.get(reverse("billing_account:overview"))

    assert response.status_code == 200
    names = _names(response)
    assert {"Own brand", "Managed brand"} <= names
    assert "Edited brand" not in names  # editors can't spend the organization's money
    assert "Unrelated brand" not in names


@pytest.mark.django_db
def test_listing_creates_no_subscription_rows(client):
    from apps.billing.models import WorkspaceSubscription

    user = User.objects.create_user(email="owner@a.com", password="x", tos_accepted_at=timezone.now())
    before = WorkspaceSubscription.objects.count()
    client.force_login(user)

    client.get(reverse("billing_account:overview"))

    assert WorkspaceSubscription.objects.count() == before
