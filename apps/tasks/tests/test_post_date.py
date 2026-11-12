"""The task modal's post date: it sets the draft's proposed date, which places it on the calendar."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.tasks import services
from apps.workspaces.models import Workspace

pytestmark = [pytest.mark.django_db]


@pytest.fixture
def workspace():
    org = Organization.objects.create(name="Acme")
    return Workspace.objects.create(organization=org, name="Acme Social", timezone="America/Sao_Paulo")


@pytest.fixture
def owner(workspace):
    user = User.objects.create_user(email="owner@example.com", password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user


@pytest.fixture
def task(workspace, owner):
    task = services.create_task(workspace, owner, title="Post the offer")
    task.post = Post.objects.create(workspace=workspace, author=owner, caption="Hi")
    task.save(update_fields=["post"])
    return task


def _update(owner, task, value):
    client = Client()
    client.force_login(owner)
    return client.post(
        reverse("tasks:task_update", kwargs={"workspace_id": task.workspace_id, "task_id": task.id}),
        {"publish_at": value},
        HTTP_HX_REQUEST="true",
    )


def test_post_date_is_read_in_the_workspace_zone_and_shown_back(owner, task):
    response = _update(owner, task, "2027-03-10T09:30")

    task.post.refresh_from_db()
    assert task.post.proposed_publish_at == datetime(2027, 3, 10, 12, 30, tzinfo=UTC)
    assert 'value="2027-03-10T09:30"' in response.content.decode()


def test_blank_post_date_clears_it(owner, task):
    task.post.proposed_publish_at = datetime(2027, 3, 10, 12, 30, tzinfo=UTC)
    task.post.save()

    _update(owner, task, "")

    task.post.refresh_from_db()
    assert task.post.proposed_publish_at is None


def test_scheduled_post_keeps_its_date(owner, task):
    scheduled = datetime(2027, 4, 1, 15, 0, tzinfo=UTC)
    task.post.scheduled_at = scheduled
    task.post.save()

    response = _update(owner, task, "2027-03-10T09:30")

    task.post.refresh_from_db()
    assert task.post.scheduled_at == scheduled
    assert task.post.proposed_publish_at is None
    assert "01/04/2027 12:00" in response.content.decode()
