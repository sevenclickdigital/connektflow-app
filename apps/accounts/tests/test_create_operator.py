"""Tests for ``manage.py create_operator``.

State and behaviour only — what the database holds after the command
runs, never the wording it printed. The one thing asserted about output
is that a generated password appears at all, because a password the
operator cannot read is the same as no account.

The separation under test: an operator is an account with no
organization, which is why it is billed nothing. Nothing here asserts
that ``is_staff`` exempts anybody, because it deliberately does not.
"""

from __future__ import annotations

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import WorkspaceSubscription
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

OPERATOR_EMAIL = "ops@example.com"


def _run(*args, **kwargs):
    kwargs.setdefault("generate_password", True)
    kwargs.setdefault("interactive", False)
    return call_command("create_operator", *args, **kwargs)


@pytest.mark.django_db
class TestCreatesAnOperator:
    def test_account_is_staff(self):
        _run(OPERATOR_EMAIL)

        user = User.objects.get(email=OPERATOR_EMAIL)
        assert user.is_staff is True
        assert user.is_active is True

    def test_not_superuser_unless_asked(self):
        _run(OPERATOR_EMAIL)

        assert User.objects.get(email=OPERATOR_EMAIL).is_superuser is False

    def test_superuser_flag_is_honoured(self):
        _run(OPERATOR_EMAIL, superuser=True)

        assert User.objects.get(email=OPERATOR_EMAIL).is_superuser is True

    def test_no_organization_is_created(self):
        """The signal that provisions one for every signup must be muted."""
        _run(OPERATOR_EMAIL)

        user = User.objects.get(email=OPERATOR_EMAIL)
        assert OrgMembership.objects.filter(user=user).count() == 0
        assert Organization.objects.count() == 0

    def test_no_workspace_is_created(self):
        """No workspace is why the account is billed nothing."""
        _run(OPERATOR_EMAIL)

        user = User.objects.get(email=OPERATOR_EMAIL)
        assert WorkspaceMembership.objects.filter(user=user).count() == 0
        assert Workspace.objects.count() == 0
        assert WorkspaceSubscription.objects.count() == 0

    def test_last_workspace_is_unset(self):
        _run(OPERATOR_EMAIL)

        assert User.objects.get(email=OPERATOR_EMAIL).last_workspace_id is None

    def test_password_is_usable_and_hashed(self, capsys):
        _run(OPERATOR_EMAIL)

        printed = capsys.readouterr().out
        password = printed.split("shown once): ")[1].splitlines()[0].strip()
        user = User.objects.get(email=OPERATOR_EMAIL)
        assert user.check_password(password)
        assert user.password != password

    def test_email_is_normalized(self):
        _run("Ops@EXAMPLE.COM")

        assert User.objects.filter(email="Ops@example.com").exists()

    def test_name_is_stored(self):
        _run(OPERATOR_EMAIL, name="Support Desk")

        assert User.objects.get(email=OPERATOR_EMAIL).name == "Support Desk"

    def test_terms_are_pre_accepted(self):
        """Otherwise the account cannot reach anything.

        ``TosAcceptanceMiddleware`` redirects every URL to the acceptance
        page while this is NULL, so an operator created without it bounces
        off the staff console they were made to use. There is no agreement
        to collect from an account the product owner created for their own
        staff.
        """
        _run(OPERATOR_EMAIL)

        assert User.objects.get(email=OPERATOR_EMAIL).tos_accepted_at is not None

    def test_new_operator_actually_reaches_the_console(self, client):
        """The end-to-end check the ToS redirect would silently fail."""
        _run(OPERATOR_EMAIL)
        client.force_login(User.objects.get(email=OPERATOR_EMAIL))

        assert client.get("/staff/").status_code == 200

    def test_refuses_to_create_without_a_password_source(self):
        """Non-interactive and no --generate-password must not invent one."""
        with pytest.raises(CommandError):
            call_command("create_operator", OPERATOR_EMAIL, interactive=False, generate_password=False)

        assert not User.objects.filter(email=OPERATOR_EMAIL).exists()


@pytest.mark.django_db
class TestIdempotence:
    def test_second_run_promotes_instead_of_failing(self):
        _run(OPERATOR_EMAIL)
        _run(OPERATOR_EMAIL)

        assert User.objects.filter(email=OPERATOR_EMAIL).count() == 1
        assert User.objects.get(email=OPERATOR_EMAIL).is_staff is True

    def test_second_run_leaves_the_password_alone(self, capsys):
        _run(OPERATOR_EMAIL)
        password = capsys.readouterr().out.split("shown once): ")[1].splitlines()[0].strip()

        _run(OPERATOR_EMAIL)

        assert User.objects.get(email=OPERATOR_EMAIL).check_password(password)

    def test_second_run_still_creates_no_organization(self):
        _run(OPERATOR_EMAIL)
        _run(OPERATOR_EMAIL)

        assert Organization.objects.count() == 0
        assert Workspace.objects.count() == 0

    def test_promoting_a_spare_account_sets_staff(self):
        """An account that belongs to nothing can join the team as it is."""
        spare = User.objects.create_user(email="spare@example.com", password="x", tos_accepted_at=timezone.now())
        OrgMembership.objects.filter(user=spare).delete()
        WorkspaceMembership.objects.filter(user=spare).delete()

        _run("spare@example.com")

        spare.refresh_from_db()
        assert spare.is_staff is True


@pytest.mark.django_db
class TestACustomerIsNeverPromoted:
    """A team account owns nothing, so a customer gets a second account instead.

    The refusal changes nothing about what the customer owns or owes.
    """

    @pytest.fixture
    def customer(self):
        user = User.objects.create_user(email="client@example.com", password="x", tos_accepted_at=timezone.now())
        OrgMembership.objects.filter(user=user).delete()
        WorkspaceMembership.objects.filter(user=user).delete()
        org = Organization.objects.create(name="Client Co")
        workspace = Workspace.objects.create(organization=org, name="Client Social")
        OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        return user, org, workspace

    def test_the_command_refuses(self, customer):
        user, _org, _workspace = customer

        with pytest.raises(CommandError):
            _run(user.email)

        user.refresh_from_db()
        assert user.is_staff is False

    def test_a_workspace_guest_is_refused_too(self, customer):
        """Belonging to one brand, with no organization, is still being a customer."""
        user, org, _workspace = customer
        OrgMembership.objects.filter(user=user, organization=org).delete()

        with pytest.raises(CommandError):
            _run(user.email)

    def test_memberships_survive_the_refusal(self, customer):
        user, org, workspace = customer

        with pytest.raises(CommandError):
            _run(user.email)

        assert OrgMembership.objects.filter(user=user, organization=org).exists()
        assert WorkspaceMembership.objects.filter(user=user, workspace=workspace).exists()
