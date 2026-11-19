"""Creating brands (workspaces)."""

from __future__ import annotations

from django.db import transaction

from apps.members.models import WorkspaceMembership
from apps.workspaces.models import Workspace


def create_brand(organization, name, owner):
    """A new brand of ``organization``, owned by ``owner``, with its billing row.

    The billing row (``ensure_subscription``) exists from the start because the
    staff console and the storage quota read it.
    """
    from apps.billing import services as billing_services

    with transaction.atomic():
        workspace = Workspace.objects.create(organization=organization, name=name)
        WorkspaceMembership.objects.create(
            user=owner, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        billing_services.ensure_subscription(workspace)
    return workspace
