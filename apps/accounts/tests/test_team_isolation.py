"""The team (support and superadmin) is kept apart from the product.

What is protected here:

- a team account lands in the administrative area and cannot open the
  product, even when a membership reached it;
- it cannot become a customer through an invitation;
- support cannot use what is reserved for a superadmin;
- the Team screen keeps the rule when it adds, changes and removes people;
- a support takeover cannot schedule or publish.

Status codes, redirect targets and stored state only; no rendered wording.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts import impersonation, operators
from apps.accounts.middleware import ImpersonationGuardMiddleware
from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post
from apps.members import services as member_services
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

CONSOLE = reverse("billing_staff:staff_console")
TEAM = reverse("accounts_staff:team")


def _team_user(email, *, superadmin=False):
    return operators.create_operator(email, password="x", superuser=superadmin)


@pytest.fixture
def support(db):
    return _team_user("support@example.com")


@pytest.fixture
def superadmin(db):
    return _team_user("root@example.com", superadmin=True)


@pytest.fixture
def workspace(db):
    org = Organization.objects.create(name="Client Co")
    return Workspace.objects.create(organization=org, name="Client Social")


@pytest.fixture
def customer(db, workspace):
    user = User.objects.create_user(email="client@example.com", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).delete()
    WorkspaceMembership.objects.filter(user=user).delete()
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user


def _give_membership(user, workspace):
    """The state the rule forbids, to check what holds when it happens anyway."""
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    user.last_workspace_id = workspace.id
    user.save(update_fields=["last_workspace_id"])


@pytest.mark.django_db
class TestATeamAccountOwnsNothing:
    def test_creating_one_provisions_no_organization(self, support):
        assert not operators.is_customer(support)

    def test_createsuperuser_provisions_no_organization(self):
        user = User.objects.create_superuser(email="su@example.com", password="x")

        assert not operators.is_customer(user)

    def test_a_customer_signing_up_still_gets_an_organization(self):
        user = User.objects.create_user(email="new@example.com", password="x")

        assert operators.is_customer(user)

    def test_it_cannot_accept_an_invitation(self, support, workspace):
        invitation = Invitation.objects.create(
            organization=workspace.organization,
            email=support.email,
            org_role=OrgMembership.OrgRole.MEMBER,
            workspace_assignments=[{"workspace_id": str(workspace.id), "role": "editor"}],
            expires_at=timezone.now() + timedelta(days=7),
        )

        with pytest.raises(ValueError):
            member_services.accept_invitation(invitation, support)

        assert not operators.is_customer(support)

    def test_a_customer_is_not_promoted(self, customer):
        with pytest.raises(operators.OperatorIsCustomerError):
            operators.promote(customer)

        customer.refresh_from_db()
        assert customer.is_staff is False


@pytest.mark.django_db
class TestATeamAccountStaysInTheAdministrativeArea:
    def test_signing_in_lands_on_the_area(self, client, support):
        client.force_login(support)

        response = client.get("/")

        assert response.status_code == 302
        assert response.url == CONSOLE

    def test_home_is_the_area_even_with_a_membership(self, client, support, workspace):
        _give_membership(support, workspace)
        client.force_login(support)

        response = client.get("/")

        assert response.url == CONSOLE

    @pytest.mark.parametrize("name", ["workspaces:dashboard", "calendar:calendar", "composer:compose"])
    def test_the_product_is_closed_even_with_a_membership(self, client, support, workspace, name):
        _give_membership(support, workspace)
        client.force_login(support)

        response = client.get(reverse(name, kwargs={"workspace_id": workspace.id}))

        assert response.status_code == 302
        assert response.url == CONSOLE

    def test_an_htmx_request_is_redirected_as_a_whole_page(self, client, support, workspace):
        _give_membership(support, workspace)
        client.force_login(support)

        response = client.get(
            reverse("calendar:calendar", kwargs={"workspace_id": workspace.id}), headers={"HX-Request": "true"}
        )

        assert response.status_code == 204
        assert response["HX-Redirect"] == CONSOLE

    def test_the_area_shows_no_tenant_context_even_with_a_membership(self, client, support, workspace):
        _give_membership(support, workspace)
        client.force_login(support)

        response = client.get(CONSOLE)

        assert response.status_code == 200
        assert response.context["request"].workspace is None
        assert response.context["request"].org is None
        assert response.context["sidebar_workspaces"] == []
        assert response.context["sidebar_org_groups"] == []

    def test_its_own_profile_stays_open(self, client, support):
        client.force_login(support)

        assert client.get(reverse("accounts:settings")).status_code == 200

    def test_a_customer_is_not_affected(self, client, customer, workspace):
        client.force_login(customer)

        response = client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id}))

        assert response.status_code == 200

    def test_a_takeover_uses_the_product_as_the_customer(self, client, support, customer, workspace):
        client.force_login(support)
        client.post(reverse("accounts:start_impersonation", kwargs={"user_id": customer.pk}))

        response = client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id}))

        assert response.status_code == 200


SUPERADMIN_ONLY = [
    "billing_staff:plans",
    "billing_staff:coupons",
    "billing_staff:goals",
    "affiliates_staff:affiliates",
    "social_accounts_staff:platforms",
    "accounts_staff:team",
]
WHOLE_TEAM = [
    "billing_staff:staff_console",
    "billing_staff:subscriptions",
    "billing_staff:impersonation_log",
    "automations_staff:template_list",
    "composer_staff:idea_templates",
    "calendar_staff:dates",
    "radar_staff:market_hashtags",
    "onboarding_staff:help_videos",
]


@pytest.mark.django_db
class TestLevels:
    @pytest.mark.parametrize("name", SUPERADMIN_ONLY)
    def test_support_is_refused_what_is_superadmin_only(self, client, support, name):
        client.force_login(support)

        assert client.get(reverse(name)).status_code == 403

    @pytest.mark.parametrize("name", SUPERADMIN_ONLY + WHOLE_TEAM)
    def test_a_superadmin_opens_every_screen(self, client, superadmin, name):
        client.force_login(superadmin)

        assert client.get(reverse(name)).status_code == 200

    @pytest.mark.parametrize("name", WHOLE_TEAM)
    def test_support_opens_service_and_content(self, client, support, name):
        client.force_login(support)

        assert client.get(reverse(name)).status_code == 200

    @pytest.mark.parametrize("name", SUPERADMIN_ONLY + WHOLE_TEAM)
    def test_a_customer_is_refused_everything(self, client, customer, name):
        client.force_login(customer)

        assert client.get(reverse(name)).status_code == 403

    def test_support_cannot_delete_a_customer(self, client, support, customer):
        client.force_login(support)

        response = client.post(
            reverse("billing_staff:delete_user", kwargs={"target_user_id": customer.pk}),
            {"confirm_email": customer.email},
        )

        assert response.status_code == 403
        assert User.objects.filter(pk=customer.pk).exists()

    def test_support_cannot_exempt_a_brand(self, client, support, workspace):
        client.force_login(support)

        response = client.post(
            reverse("billing_staff:toggle_courtesy", kwargs={"target_workspace_id": workspace.id}),
            {"action": "grant"},
        )

        assert response.status_code == 403

    def test_support_cannot_edit_an_account(self, client, support, workspace):
        client.force_login(support)

        response = client.post(
            reverse("billing_staff:edit_account", kwargs={"org_id": workspace.organization_id}), {"name": "Taken"}
        )

        assert response.status_code == 403
        workspace.organization.refresh_from_db()
        assert workspace.organization.name == "Client Co"

    def test_support_can_send_a_password_reset(self, client, support, customer):
        client.force_login(support)

        response = client.post(reverse("billing_staff:send_password_reset", kwargs={"target_user_id": customer.pk}))

        assert response.status_code == 302


@pytest.mark.django_db
class TestTheTeamScreen:
    def test_adding_creates_an_account_that_owns_nothing(self, client, superadmin):
        client.force_login(superadmin)

        client.post(reverse("accounts_staff:team_add"), {"email": "New@Example.com", "name": "New", "role": "support"})

        added = User.objects.get(email__iexact="new@example.com")
        assert added.is_staff and not added.is_superuser
        assert not operators.is_customer(added)
        assert len(mail.outbox) == 1

    def test_adding_a_superadmin(self, client, superadmin):
        client.force_login(superadmin)

        client.post(reverse("accounts_staff:team_add"), {"email": "boss@example.com", "role": "superadmin"})

        assert User.objects.get(email="boss@example.com").is_superuser

    def test_a_customer_address_is_refused(self, client, superadmin, customer):
        client.force_login(superadmin)

        client.post(reverse("accounts_staff:team_add"), {"email": customer.email, "role": "superadmin"})

        customer.refresh_from_db()
        assert not customer.is_staff and not customer.is_superuser

    def test_an_invalid_address_creates_nothing(self, client, superadmin):
        client.force_login(superadmin)
        before = User.objects.count()

        client.post(reverse("accounts_staff:team_add"), {"email": "not-an-address", "role": "support"})

        assert User.objects.count() == before

    def test_changing_the_level(self, client, superadmin, support):
        client.force_login(superadmin)

        client.post(reverse("accounts_staff:team_set_role", kwargs={"user_id": support.pk}), {"role": "superadmin"})

        support.refresh_from_db()
        assert support.is_superuser

    def test_nobody_changes_their_own_account(self, client, superadmin):
        client.force_login(superadmin)

        demote = client.post(
            reverse("accounts_staff:team_set_role", kwargs={"user_id": superadmin.pk}), {"role": "support"}
        )
        remove = client.post(reverse("accounts_staff:team_remove", kwargs={"user_id": superadmin.pk}))

        assert demote.status_code == remove.status_code == 403
        superadmin.refresh_from_db()
        assert superadmin.is_superuser

    def test_removing_deletes_the_account(self, client, superadmin, support):
        client.force_login(superadmin)

        client.post(reverse("accounts_staff:team_remove", kwargs={"user_id": support.pk}))

        assert not User.objects.filter(pk=support.pk).exists()

    def test_a_customer_cannot_be_removed_through_it(self, client, superadmin, customer):
        client.force_login(superadmin)

        response = client.post(reverse("accounts_staff:team_remove", kwargs={"user_id": customer.pk}))

        assert response.status_code == 404
        assert User.objects.filter(pk=customer.pk).exists()

    def test_the_last_active_superadmin_is_kept(self, superadmin):
        with pytest.raises(operators.LastSuperadminError):
            operators.set_superadmin(superadmin, False)
        with pytest.raises(operators.LastSuperadminError):
            operators.remove_operator(superadmin)

        superadmin.refresh_from_db()
        assert superadmin.is_superuser


@pytest.mark.django_db
class TestATakeoverCannotPublish:
    @pytest.fixture
    def draft(self, customer, workspace):
        account = SocialAccount.objects.create(
            workspace=workspace,
            platform="facebook",
            account_platform_id="page-1",
            account_name="Page",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        post = Post.objects.create(workspace=workspace, author=customer, caption="Hello")
        return PlatformPost.objects.create(post=post, social_account=account)

    def test_scheduling_is_refused(self, draft):
        with impersonation.block_publishing(), pytest.raises(impersonation.PublishingBlockedError):
            draft.transition_to("scheduled")

    def test_a_row_given_the_status_directly_is_refused(self, draft):
        draft.status = PlatformPost.Status.SCHEDULED

        with impersonation.block_publishing(), pytest.raises(impersonation.PublishingBlockedError):
            draft.save()

        draft.refresh_from_db()
        assert draft.status == PlatformPost.Status.DRAFT

    def test_a_row_created_as_scheduled_is_refused(self, draft):
        with impersonation.block_publishing(), pytest.raises(impersonation.PublishingBlockedError):
            PlatformPost.objects.create(
                post=draft.post,
                social_account=draft.social_account,
                format="story",
                status=PlatformPost.Status.SCHEDULED,
            )

    def test_an_already_scheduled_row_still_saves(self, draft):
        draft.transition_to("scheduled")
        draft.save()

        with impersonation.block_publishing():
            draft.platform_specific_caption = "Edited"
            draft.save()

        draft.refresh_from_db()
        assert draft.platform_specific_caption == "Edited"

    def test_drafts_are_untouched_by_the_block(self, draft):
        with impersonation.block_publishing():
            draft.platform_specific_caption = "Edited"
            draft.save()

        assert draft.status == PlatformPost.Status.DRAFT

    def test_outside_a_takeover_scheduling_works(self, draft):
        draft.transition_to("scheduled")
        draft.save()

        assert not impersonation.publishing_blocked()

    def test_the_block_covers_exactly_the_takeover_request(self, client, support, customer):
        """Read from inside the middleware chain: on during a takeover, off before and after."""
        seen = []

        def view(request):
            seen.append(impersonation.publishing_blocked())

        middleware = ImpersonationGuardMiddleware(view)
        client.force_login(support)

        def serve():
            request = RequestFactory().get("/")
            request.COOKIES = {key: morsel.value for key, morsel in client.cookies.items()}
            request.session = client.session
            middleware(request)

        serve()
        client.post(reverse("accounts:start_impersonation", kwargs={"user_id": customer.pk}))
        serve()

        assert seen == [False, True]
        assert not impersonation.publishing_blocked()

    def test_the_refusal_is_answered_with_a_message(self, client, support, customer):
        client.force_login(support)
        client.post(reverse("accounts:start_impersonation", kwargs={"user_id": customer.pk}))
        request = RequestFactory().post("/", headers={"referer": "http://testserver/somewhere/"})
        request.session = client.session
        request._messages = type("Sink", (), {"add": lambda *a, **k: None})()

        response = ImpersonationGuardMiddleware(lambda r: None).process_exception(
            request, impersonation.PublishingBlockedError()
        )

        assert response.status_code == 302
        assert response.url == "http://testserver/somewhere/"

    def test_other_errors_are_left_alone(self):
        request = RequestFactory().get("/")

        assert ImpersonationGuardMiddleware(lambda r: None).process_exception(request, ValueError()) is None
