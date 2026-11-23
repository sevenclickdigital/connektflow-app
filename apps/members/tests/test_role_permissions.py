"""Roles and permissions of an organization: what the page saves, who reaches it and what a member then may do."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members import services
from apps.members.models import BUILTIN_ROLE_PERMISSIONS, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

ROLE = WorkspaceMembership.WorkspaceRole


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Agency")


@pytest.fixture
def brand(org):
    return Workspace.objects.create(organization=org, name="Brand")


def _person(email, org, brand, role, org_role=OrgMembership.OrgRole.MEMBER):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).delete()
    OrgMembership.objects.create(user=user, organization=org, org_role=org_role)
    WorkspaceMembership.objects.create(user=user, workspace=brand, workspace_role=role)
    return user


def _permissions(user, brand):
    return WorkspaceMembership.objects.get(user=user, workspace=brand).effective_permissions


def _url(org):
    return reverse("organizations:role_permissions", kwargs={"organization_id": org.id})


@pytest.mark.django_db
def test_an_adjusted_role_changes_what_its_members_may_do(org, brand):
    editor = _person("editor@a.com", org, brand, ROLE.EDITOR)
    wanted = {k for k, v in BUILTIN_ROLE_PERMISSIONS["editor"].items() if v} - {"view_analytics"} | {"publish_directly"}

    services.set_role_permissions(org, {"editor": wanted})

    perms = _permissions(editor, brand)
    assert perms["view_analytics"] is False
    assert perms["publish_directly"] is True
    assert perms["create_posts"] is True
    # Only the differences are stored, so the rest follows the product's defaults.
    org.refresh_from_db()
    assert org.role_permissions == {"editor": {"view_analytics": False, "publish_directly": True}}


@pytest.mark.django_db
def test_the_adjustment_stays_inside_the_organization(org, brand):
    other = Organization.objects.create(name="Other")
    other_brand = Workspace.objects.create(organization=other, name="Other Brand")
    outsider = _person("editor@b.com", other, other_brand, ROLE.EDITOR)

    services.set_role_permissions(org, {"editor": set()})

    assert _permissions(outsider, other_brand) == BUILTIN_ROLE_PERMISSIONS["editor"]


@pytest.mark.django_db
def test_the_owner_role_is_never_adjusted(org, brand):
    owner = _person("owner@a.com", org, brand, ROLE.OWNER, OrgMembership.OrgRole.OWNER)

    services.set_role_permissions(org, {"owner": set(), "viewer": set()})
    # Even a stored value for the owner is not read.
    org.role_permissions = {"owner": {"create_posts": False}}
    org.save(update_fields=["role_permissions"])

    assert all(_permissions(owner, brand).values())


@pytest.mark.django_db
def test_a_role_back_on_its_defaults_stores_nothing(org):
    services.set_role_permissions(org, {"viewer": {"view_analytics", "create_posts"}})
    services.set_role_permissions(org, {"viewer": {"view_analytics"}})

    org.refresh_from_db()
    assert org.role_permissions is None


@pytest.mark.django_db
def test_the_page_saves_what_was_ticked_and_ignores_what_it_does_not_know(client, org, brand):
    admin = _person("admin@a.com", org, brand, ROLE.OWNER, OrgMembership.OrgRole.ADMIN)
    contributor = _person("c@a.com", org, brand, ROLE.CONTRIBUTOR)
    client.force_login(admin)
    assert client.get(_url(org)).status_code == 200

    posted = {
        f"role_{role}": [k for k, v in BUILTIN_ROLE_PERMISSIONS[role].items() if v]
        for role in services.ADJUSTABLE_ROLES
    }
    posted["role_contributor"] = [*posted["role_contributor"], "view_analytics", "made_up_permission"]
    posted["role_owner"] = []
    client.post(_url(org), posted)

    org.refresh_from_db()
    assert org.role_permissions == {"contributor": {"view_analytics": True}}
    assert _permissions(contributor, brand)["view_analytics"] is True

    client.post(_url(org), {"action": "reset"})
    org.refresh_from_db()
    assert org.role_permissions is None


@pytest.mark.django_db
def test_only_an_owner_or_admin_of_that_organization_reaches_the_page(client, org, brand):
    member = _person("member@a.com", org, brand, ROLE.MANAGER)
    other = Organization.objects.create(name="Other")
    other_brand = Workspace.objects.create(organization=other, name="Other Brand")
    outsider = _person("owner@b.com", other, other_brand, ROLE.OWNER, OrgMembership.OrgRole.OWNER)

    for user in (member, outsider):
        client.force_login(user)
        assert client.get(_url(org)).status_code == 404
        assert client.post(_url(org), {"role_viewer": ["create_posts"]}).status_code == 404
    org.refresh_from_db()
    assert org.role_permissions is None
