"""Moving a brand to another organization of the same person.

Protected here: who may move what (tenant isolation), what goes with the
brand, and the billing rules — the brand must never keep being charged to,
or exempted by, the organization it left.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import services as billing_services
from apps.billing.models import OrganizationPlan, WorkspaceStorageAddon, WorkspaceSubscription
from apps.media_library.models import MediaAsset, MediaFolder
from apps.members.models import CustomRole, Invitation, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces import services
from apps.workspaces.models import Workspace

URL = reverse("organizations:move_brand")
STATUS = WorkspaceSubscription.Status


def _join(user, organization, role=OrgMembership.OrgRole.OWNER):
    OrgMembership.objects.create(user=user, organization=organization, org_role=role)


@pytest.fixture
def origin(org_owner, organization):
    return organization


@pytest.fixture
def destination(org_owner):
    organization = Organization.objects.create(name="Second Organization")
    _join(org_owner, organization)
    return organization


@pytest.fixture
def brand(origin, org_owner):
    return services.create_brand(origin, "Brand", org_owner)


def _post(client, brand, destination):
    return client.post(URL, {"brand": str(brand.pk), "destination": str(destination.pk)})


def _plan(organization, brands=1):
    return OrganizationPlan.objects.create(
        organization=organization,
        status=STATUS.ACTIVE,
        brands=brands,
        current_period_end=timezone.now() + timedelta(days=30),
    )


@pytest.mark.django_db
class TestWhatGoesWithTheBrand:
    def test_the_brand_changes_organization(self, client, org_owner, brand, destination):
        client.force_login(org_owner)

        response = _post(client, brand, destination)

        assert response.status_code == 302
        brand.refresh_from_db()
        assert brand.organization_id == destination.pk

    def test_its_media_follows_and_the_shared_media_stays(self, org_owner, origin, brand, destination):
        mine = MediaAsset.objects.create(organization=origin, workspace=brand, filename="a.png", media_type="image")
        folder = MediaFolder.objects.create(organization=origin, workspace=brand, name="Folder")
        shared = MediaFolder.objects.create(organization=origin, name="Shared")

        services.move_brand(brand, destination)

        mine.refresh_from_db()
        folder.refresh_from_db()
        shared.refresh_from_db()
        assert mine.organization_id == folder.organization_id == destination.pk
        assert shared.organization_id == origin.pk

    def test_people_stay_linked_and_lose_the_old_organization_s_custom_role(self, origin, brand, destination):
        guest = User.objects.create_user(email="guest@example.com", password="x")
        role = CustomRole.objects.create(organization=origin, name="Reviewer", permissions={})
        membership = WorkspaceMembership.objects.create(
            user=guest, workspace=brand, workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR, custom_role=role
        )

        services.move_brand(brand, destination)

        membership.refresh_from_db()
        assert membership.custom_role is None
        assert membership.workspace_role == WorkspaceMembership.WorkspaceRole.EDITOR
        assert not OrgMembership.objects.filter(user=guest, organization=destination).exists()

    def test_pending_invitations_of_the_old_organization_stop_naming_it(self, origin, brand, destination):
        other = Workspace.objects.create(organization=origin, name="Other")
        invitation = Invitation.objects.create(
            organization=origin,
            email="new@example.com",
            org_role=OrgMembership.OrgRole.MEMBER,
            workspace_assignments=[
                {"workspace_id": str(brand.pk), "role": "editor"},
                {"workspace_id": str(other.pk), "role": "viewer"},
            ],
            expires_at=timezone.now() + timedelta(days=7),
        )

        services.move_brand(brand, destination)

        invitation.refresh_from_db()
        assert invitation.workspace_assignments == [{"workspace_id": str(other.pk), "role": "viewer"}]


@pytest.mark.django_db
class TestWhoMayMove:
    def test_not_into_an_organization_the_person_only_belongs_to(self, client, org_owner, brand):
        elsewhere = Organization.objects.create(name="Elsewhere")
        _join(org_owner, elsewhere, OrgMembership.OrgRole.MEMBER)
        client.force_login(org_owner)

        assert _post(client, brand, elsewhere).status_code == 404
        brand.refresh_from_db()
        assert brand.organization.name == "Test Organization"

    def test_not_into_a_stranger_s_organization(self, client, org_owner, brand):
        stranger = Organization.objects.create(name="Stranger")
        client.force_login(org_owner)

        assert _post(client, brand, stranger).status_code == 404

    def test_not_a_stranger_s_brand(self, client, org_owner, destination):
        stranger = Organization.objects.create(name="Stranger")
        theirs = Workspace.objects.create(organization=stranger, name="Theirs")
        client.force_login(org_owner)

        assert _post(client, theirs, destination).status_code == 404
        theirs.refresh_from_db()
        assert theirs.organization_id == stranger.pk

    def test_not_a_brand_of_an_organization_the_person_only_manages_a_brand_in(self, client, org_owner, destination):
        elsewhere = Organization.objects.create(name="Elsewhere")
        theirs = Workspace.objects.create(organization=elsewhere, name="Theirs")
        WorkspaceMembership.objects.create(
            user=org_owner, workspace=theirs, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        client.force_login(org_owner)

        assert _post(client, theirs, destination).status_code == 404

    def test_a_visitor_is_sent_to_log_in(self, client, brand, destination):
        response = _post(client, brand, destination)

        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_ids_that_are_not_ids_are_a_404(self, client, org_owner):
        client.force_login(org_owner)

        assert client.post(URL, {"brand": "x", "destination": str(uuid.uuid4())}).status_code == 404

    def test_get_does_not_move(self, client, org_owner, brand, destination):
        client.force_login(org_owner)

        assert client.get(URL, {"brand": str(brand.pk), "destination": str(destination.pk)}).status_code == 405


@pytest.mark.django_db
class TestRefusals:
    def test_the_same_organization(self, brand, origin):
        with pytest.raises(services.BrandMoveError):
            services.move_brand(brand, origin)

    def test_an_archived_brand(self, brand, destination):
        brand.is_archived = True
        brand.save(update_fields=["is_archived"])

        with pytest.raises(services.BrandMoveError):
            services.move_brand(brand, destination)

    def test_an_organization_scheduled_for_deletion(self, brand, destination):
        destination.deletion_requested_at = timezone.now()
        destination.save(update_fields=["deletion_requested_at"])

        with pytest.raises(services.BrandMoveError):
            services.move_brand(brand, destination)

    @pytest.mark.parametrize("status", [STATUS.ACTIVE, STATUS.PAST_DUE])
    def test_a_brand_charged_on_its_own_subscription(self, client, org_owner, brand, destination, status):
        """The charge would keep going to the organization it left."""
        row = billing_services.ensure_subscription(brand)
        row.status = status
        row.gateway_subscription_id = "sub_123"
        row.save(update_fields=["status", "gateway_subscription_id"])
        client.force_login(org_owner)

        response = _post(client, brand, destination)

        assert response.status_code == 302
        brand.refresh_from_db()
        assert brand.organization.name == "Test Organization"

    def test_a_brand_with_extra_disk_contracted(self, brand, destination):
        """Extra disk is charged to the organization the brand is in."""
        WorkspaceStorageAddon.objects.create(
            workspace=brand, quantity=5, unit_price_cents=200, status=WorkspaceStorageAddon.Status.ACTIVE
        )

        with pytest.raises(services.BrandMoveError):
            services.move_brand(brand, destination)

        brand.refresh_from_db()
        assert brand.organization.name == "Test Organization"

    def test_cancelled_extra_disk_does_not_hold_the_brand(self, brand, destination):
        WorkspaceStorageAddon.objects.create(
            workspace=brand, quantity=5, unit_price_cents=200, status=WorkspaceStorageAddon.Status.CANCELED
        )

        services.move_brand(brand, destination)

        brand.refresh_from_db()
        assert brand.organization_id == destination.pk


@pytest.mark.django_db
class TestBillingFollowsTheDestination:
    def test_a_brand_not_contracted_arrives_not_contracted(self, brand, destination):
        services.move_brand(brand, destination)

        assert billing_services.ensure_subscription(brand).status == STATUS.PENDING

    def test_a_covered_brand_gives_its_place_back_to_the_plan_it_leaves(self, origin, org_owner, brand, destination):
        plan = _plan(origin)
        billing_services.sync_plan_coverage(plan)
        waiting = services.create_brand(origin, "Waiting", org_owner)
        assert billing_services.ensure_subscription(waiting).status == STATUS.PENDING

        services.move_brand(brand, destination)

        moved = WorkspaceSubscription.objects.get(workspace=brand)
        assert moved.plan is None and moved.status == STATUS.PENDING
        taken = WorkspaceSubscription.objects.get(workspace=waiting)
        assert taken.plan_id == plan.pk and taken.status == STATUS.ACTIVE

    def test_the_destination_s_plan_covers_it_when_it_has_a_brand_left(self, brand, destination):
        plan = _plan(destination)

        services.move_brand(brand, destination)

        moved = WorkspaceSubscription.objects.get(workspace=brand)
        assert moved.plan_id == plan.pk and moved.status == STATUS.ACTIVE

    def test_a_full_plan_at_the_destination_does_not_cover_it(self, org_owner, brand, destination):
        plan = _plan(destination)
        services.create_brand(destination, "Already there", org_owner)
        billing_services.sync_plan_coverage(plan)

        services.move_brand(brand, destination)

        moved = WorkspaceSubscription.objects.get(workspace=brand)
        assert moved.plan is None and moved.status == STATUS.PENDING

    def test_an_exemption_is_kept_when_the_destination_has_one_unused(self, brand, destination):
        billing_services.grant_courtesy(brand)
        profile = billing_services.ensure_billing_profile(destination)
        profile.courtesy_workspace_quota = 1
        profile.save(update_fields=["courtesy_workspace_quota"])

        services.move_brand(brand, destination)

        assert WorkspaceSubscription.objects.get(workspace=brand).status == STATUS.COURTESY

    def test_an_exemption_is_not_carried_into_an_organization_with_none_left(self, brand, destination):
        """Moving is not a way around an exemption's limit or end date."""
        billing_services.grant_courtesy(brand)

        services.move_brand(brand, destination)

        assert WorkspaceSubscription.objects.get(workspace=brand).status == STATUS.PENDING


@pytest.mark.django_db
class TestTheScreenOffersIt:
    def test_only_with_another_organization_to_move_to(self, client, org_owner, brand):
        # Only the organization of the fixture, not the one the sign-up provisioned.
        OrgMembership.objects.filter(user=org_owner).exclude(organization=brand.organization).delete()
        client.force_login(org_owner)

        alone = client.get(reverse("organizations:workspaces"))
        assert not any(item["can_move"] for group in alone.context["org_groups"] for item in group["brands"])

        second = Organization.objects.create(name="Second")
        _join(org_owner, second)
        both = client.get(reverse("organizations:workspaces"))
        assert any(item["can_move"] for group in both.context["org_groups"] for item in group["brands"])
        assert {d["name"] for d in both.context["move_destinations"]} == {"Test Organization", "Second"}
