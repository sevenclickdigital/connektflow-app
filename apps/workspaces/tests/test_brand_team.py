"""Brand settings → Manage team: who may manage it, and what they may do."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

ROLE = WorkspaceMembership.WorkspaceRole


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Agency")


@pytest.fixture
def brand(org):
    return Workspace.objects.create(organization=org, name="Brand")


def _person(email, org, *, brand=None, role=None, org_role=OrgMembership.OrgRole.MEMBER):
    user = User.objects.create_user(email=email, password="x", name=email.split("@")[0], tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=org, org_role=org_role)
    if brand is not None:
        WorkspaceMembership.objects.create(user=user, workspace=brand, workspace_role=role)
    return user


def _url(brand):
    return reverse("workspaces:team", kwargs={"workspace_id": brand.id})


def _role(user, brand):
    m = WorkspaceMembership.objects.filter(user=user, workspace=brand).first()
    return m.workspace_role if m else None


@pytest.mark.django_db
def test_a_manager_adds_an_organization_member(client, org, brand):
    manager = _person("manager@a.com", org, brand=brand, role=ROLE.MANAGER)
    newcomer = _person("new@a.com", org)
    client.force_login(manager)

    client.post(_url(brand), {"action": "add", "user_id": newcomer.id, "role": ROLE.EDITOR})

    assert _role(newcomer, brand) == ROLE.EDITOR


@pytest.mark.django_db
@pytest.mark.parametrize("role", [ROLE.EDITOR, ROLE.CONTRIBUTOR, ROLE.CLIENT, ROLE.VIEWER])
def test_below_manager_the_page_does_not_exist(client, org, brand, role):
    user = _person("u@a.com", org, brand=brand, role=role)
    client.force_login(user)

    assert client.get(_url(brand)).status_code == 404
    assert client.post(_url(brand), {"action": "remove", "user_id": user.id}).status_code == 404


@pytest.mark.django_db
def test_someone_outside_the_brand_and_organization_is_refused(client, org, brand):
    stranger_org = Organization.objects.create(name="Elsewhere")
    stranger = _person("x@b.com", stranger_org)
    client.force_login(stranger)

    # The RBAC middleware refuses a brand the user has no link to before the view runs.
    assert client.get(_url(brand)).status_code in (403, 404)


@pytest.mark.django_db
def test_a_person_from_another_organization_cannot_be_added(client, org, brand):
    manager = _person("manager@a.com", org, brand=brand, role=ROLE.MANAGER)
    outsider = _person("out@b.com", Organization.objects.create(name="Other"))
    client.force_login(manager)

    client.post(_url(brand), {"action": "add", "user_id": outsider.id, "role": ROLE.VIEWER})

    assert _role(outsider, brand) is None


@pytest.mark.django_db
def test_a_manager_cannot_grant_owner_or_touch_an_owner(client, org, brand):
    owner = _person("owner@a.com", org, brand=brand, role=ROLE.OWNER)
    manager = _person("manager@a.com", org, brand=brand, role=ROLE.MANAGER)
    member = _person("m@a.com", org, brand=brand, role=ROLE.EDITOR)
    client.force_login(manager)

    client.post(_url(brand), {"action": "change_role", "user_id": member.id, "role": ROLE.OWNER})
    client.post(_url(brand), {"action": "remove", "user_id": owner.id})

    assert _role(member, brand) == ROLE.EDITOR
    assert _role(owner, brand) == ROLE.OWNER


@pytest.mark.django_db
def test_the_last_owner_cannot_be_removed_or_demoted(client, org, brand):
    owner = _person("owner@a.com", org, brand=brand, role=ROLE.OWNER)
    client.force_login(owner)

    client.post(_url(brand), {"action": "change_role", "user_id": owner.id, "role": ROLE.MANAGER})
    client.post(_url(brand), {"action": "remove", "user_id": owner.id})

    assert _role(owner, brand) == ROLE.OWNER


@pytest.mark.django_db
def test_removing_from_the_brand_keeps_other_brands(client, org, brand):
    owner = _person("owner@a.com", org, brand=brand, role=ROLE.OWNER)
    other = Workspace.objects.create(organization=org, name="Other brand")
    member = _person("m@a.com", org, brand=brand, role=ROLE.EDITOR)
    WorkspaceMembership.objects.create(user=member, workspace=other, workspace_role=ROLE.EDITOR)
    client.force_login(owner)

    client.post(_url(brand), {"action": "remove", "user_id": member.id})

    assert _role(member, brand) is None
    assert _role(member, other) == ROLE.EDITOR


@pytest.mark.django_db
def test_an_owner_of_a_brand_in_another_organization_manages_its_team(client, org, brand):
    # Added to this brand only, not to the organization that owns it.
    guest = User.objects.create_user(email="guest@b.com", password="x", tos_accepted_at=timezone.now())
    WorkspaceMembership.objects.create(user=guest, workspace=brand, workspace_role=ROLE.OWNER)
    member = _person("m@a.com", org)
    client.force_login(guest)

    assert client.get(_url(brand)).status_code == 200
    client.post(_url(brand), {"action": "add", "user_id": member.id, "role": ROLE.VIEWER})

    assert _role(member, brand) == ROLE.VIEWER


@pytest.mark.django_db
def test_a_pending_invitation_is_listed_worded_around_the_brand_and_can_be_canceled(client, org, brand, mailoutbox):
    from apps.members.models import Invitation

    manager = _person("manager@a.com", org, brand=brand, role=ROLE.MANAGER)
    other_brand = Workspace.objects.create(organization=org, name="Other Brand")
    client.force_login(manager)

    client.post(_url(brand), {"action": "invite", "name": "  Ana   Lima ", "email": "guest@b.com", "role": ROLE.EDITOR})

    invitation = Invitation.objects.get(email="guest@b.com")
    assert invitation.name == "Ana Lima"
    assert "Ana Lima" in mailoutbox[-1].body
    assert invitation.brand.name == "Brand"
    assert "Brand" in mailoutbox[-1].subject and "Agency" not in mailoutbox[-1].subject
    pending = client.get(_url(brand)).context["pending_invitations"]
    assert [item["invitation"].id for item in pending] == [invitation.id]

    # Another brand's team page neither lists it nor cancels it.
    other_manager = _person("other@a.com", org, brand=other_brand, role=ROLE.MANAGER)
    client.force_login(other_manager)
    assert client.get(_url(other_brand)).context["pending_invitations"] == []
    client.post(_url(other_brand), {"action": "revoke_invite", "invitation_id": invitation.id})
    invitation.refresh_from_db()
    assert not invitation.is_expired

    client.force_login(manager)
    assert client.post(_url(brand), {"action": "revoke_invite", "invitation_id": "not-a-uuid"}).status_code == 302
    client.post(_url(brand), {"action": "revoke_invite", "invitation_id": invitation.id})
    assert client.get(_url(brand)).context["pending_invitations"] == []


@pytest.mark.django_db
def test_the_invited_name_becomes_the_name_of_an_account_that_has_none(org, brand):
    from apps.members import services
    from apps.members.models import Invitation

    manager = _person("manager@a.com", org, brand=brand, role=ROLE.MANAGER)
    invitation = services.create_invitation(
        org=org,
        email="guest@b.com",
        org_role=OrgMembership.OrgRole.MEMBER,
        workspace_assignments=[{"workspace_id": str(brand.id), "role": ROLE.EDITOR}],
        invited_by=manager,
        name="Ana Lima",
    )
    unnamed = User.objects.create_user(email="guest@b.com", password="x", tos_accepted_at=timezone.now())
    User.objects.filter(pk=unnamed.pk).update(name="")
    unnamed.refresh_from_db()

    services.accept_invitation(Invitation.objects.get(pk=invitation.pk), unnamed)

    unnamed.refresh_from_db()
    assert unnamed.name == "Ana Lima"
