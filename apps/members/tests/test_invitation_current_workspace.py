"""Accepting an invitation must not move the user out of the workspace they use.

``User.last_workspace_id`` is the workspace an OAuth MCP grant acts in, so an
invitation from another organization cannot be what retargets a connected agent.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.members.services import accept_invitation
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


def _workspace(name):
    return Workspace.objects.create(name=name, organization=Organization.objects.create(name=f"{name} Org"))


def _invitation(workspace, email):
    return Invitation.objects.create(
        organization=workspace.organization,
        email=email,
        org_role=OrgMembership.OrgRole.MEMBER,
        workspace_assignments=[{"workspace_id": str(workspace.id), "role": "editor"}],
        expires_at=timezone.now() + timedelta(days=7),
    )


@pytest.fixture
def invitee(db):
    return User.objects.create_user(email="invitee@example.com", password="x", tos_accepted_at=timezone.now())


@pytest.mark.django_db
class TestAcceptInvitationKeepsCurrentWorkspace:
    def test_workspace_in_use_stays_current(self, invitee):
        current = _workspace("Current")
        WorkspaceMembership.objects.create(user=invitee, workspace=current, workspace_role="owner")
        invitee.last_workspace_id = current.id
        invitee.save(update_fields=["last_workspace_id"])
        invited_to = _workspace("Invited")

        accept_invitation(_invitation(invited_to, invitee.email), invitee)

        invitee.refresh_from_db()
        assert invitee.last_workspace_id == current.id
        assert WorkspaceMembership.objects.filter(user=invitee, workspace=invited_to).exists()

    def test_user_without_usable_workspace_lands_on_the_invited_one(self, invitee):
        gone = _workspace("Archived")
        WorkspaceMembership.objects.create(user=invitee, workspace=gone, workspace_role="owner")
        gone.is_archived = True
        gone.save(update_fields=["is_archived"])
        invitee.last_workspace_id = gone.id
        invitee.save(update_fields=["last_workspace_id"])
        invited_to = _workspace("Invited")

        accept_invitation(_invitation(invited_to, invitee.email), invitee)

        invitee.refresh_from_db()
        assert invitee.last_workspace_id == invited_to.id
