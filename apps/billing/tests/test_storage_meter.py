"""The disk meter in the composer's media picker."""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing import services
from apps.billing.models import WorkspaceAddon
from apps.billing.templatetags.billing_extras import storage_size
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

GB = WorkspaceAddon.BYTES_PER_GB


@override_settings(LANGUAGE_CODE="en-us", USE_THOUSAND_SEPARATOR=False)
@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (0, "0 MB"),
        (2_500_000, "2.5 MB"),
        (25_400_000, "25 MB"),
        (1_200_000_000, "1.2 GB"),
        (9_986_000_000, "10 GB"),
        (10 * GB, "10 GB"),
    ],
)
def test_storage_size_keeps_small_figures_readable(num_bytes, expected):
    assert storage_size(num_bytes) == expected


@pytest.fixture
def setup(db):
    cache.clear()
    org = Organization.objects.create(name="Acme")
    workspace = Workspace.objects.create(organization=org, name="Acme Social")
    user = User.objects.create_user(email="o@example.com", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    client = Client()
    client.force_login(user)
    return workspace, user, client


def _picker(client, workspace):
    return client.get(reverse("composer:media_picker", kwargs={"workspace_id": workspace.id})).content.decode()


def test_the_picker_shows_how_full_the_workspace_is(setup):
    workspace, user, client = setup
    sub = services.ensure_subscription(workspace)
    sub.base_storage_bytes = 10 * GB
    sub.save()
    MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        uploaded_by=user,
        filename="big.mp4",
        media_type=MediaAsset.MediaType.VIDEO,
        file_size=GB,
    )

    assert 'aria-valuenow="10"' in _picker(client, workspace)


def test_no_meter_without_a_subscription(setup):
    workspace, _user, client = setup

    assert 'role="progressbar"' not in _picker(client, workspace)
