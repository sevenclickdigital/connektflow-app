"""The Publish page's status board: what it lists and which drops it allows."""

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.approvals.models import ApprovalAction
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_english")]

Role = WorkspaceMembership.WorkspaceRole
S = PlatformPost.Status


@pytest.fixture
def _english(settings):
    settings.LANGUAGE_CODE = "en-us"


@pytest.fixture
def workspace():
    org = Organization.objects.create(name="Acme")
    return Workspace.objects.create(organization=org, name="Acme Social")


def _member(workspace, email, role):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    OrgMembership.objects.get_or_create(
        user=user, organization=workspace.organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
    )
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    client = Client()
    client.force_login(user)
    return user, client


@pytest.fixture
def owner(workspace):
    return _member(workspace, "owner@example.com", Role.OWNER)


@pytest.fixture
def page(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="fb-1",
        account_name="The Page",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _pp(workspace, author, account, status, **extra):
    post = Post.objects.create(workspace=workspace, author=author, caption=f"Caption {status}")
    return PlatformPost.objects.create(post=post, social_account=account, status=status, **extra)


def _move(client, pp, column):
    return client.post(
        reverse("calendar:status_board_move", kwargs={"workspace_id": pp.post.workspace_id, "platform_post_id": pp.id}),
        {"column": column},
    )


def _toast(response):
    return json.loads(response["HX-Trigger"])["showToast"]


def test_the_board_lists_this_workspaces_posts_by_stage(workspace, owner, page):
    user, client = owner
    _pp(workspace, user, page, S.DRAFT)
    _pp(workspace, user, page, S.PENDING_CLIENT)
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    other_page = SocialAccount.objects.create(
        workspace=other, platform="facebook", account_platform_id="fb-2", account_name="Not ours"
    )
    _pp(other, user, other_page, S.DRAFT)

    body = client.get(
        reverse("calendar:calendar", kwargs={"workspace_id": workspace.id}), {"mode": "board"}
    ).content.decode()

    assert "Caption draft" in body and "Caption pending_client" in body
    assert "Not ours" not in body


def test_dropping_a_draft_on_approval_submits_it(workspace, owner, page):
    user, client = owner
    pp = _pp(workspace, user, page, S.DRAFT)

    response = _move(client, pp, "review")

    pp.refresh_from_db()
    assert pp.status == S.PENDING_REVIEW
    assert ApprovalAction.objects.filter(platform_post=pp, action=ApprovalAction.ActionType.SUBMITTED).exists()
    assert _toast(response)["tone"] == "success"


def test_approving_needs_the_approve_permission(workspace, owner, page):
    user, _client = owner
    _editor, editor_client = _member(workspace, "editor@example.com", Role.EDITOR)
    pp = _pp(workspace, user, page, S.PENDING_REVIEW)

    response = _move(editor_client, pp, "approved")

    pp.refresh_from_db()
    assert pp.status == S.PENDING_REVIEW
    assert _toast(response)["tone"] == "error"


def test_scheduling_needs_a_future_time(workspace, owner, page):
    user, client = owner
    undated = _pp(workspace, user, page, S.APPROVED)
    dated = _pp(workspace, user, page, S.APPROVED, scheduled_at=timezone.now() + timedelta(days=2))

    _move(client, undated, "scheduled")
    _move(client, dated, "scheduled")

    undated.refresh_from_db()
    dated.refresh_from_db()
    assert undated.status == S.APPROVED
    assert dated.status == S.SCHEDULED


def test_back_to_draft_drops_the_time(workspace, owner, page):
    user, client = owner
    pp = _pp(workspace, user, page, S.SCHEDULED, scheduled_at=timezone.now() + timedelta(days=1))

    _move(client, pp, "draft")

    pp.refresh_from_db()
    assert pp.status == S.DRAFT and pp.scheduled_at is None


def test_nothing_can_be_dropped_on_published(workspace, owner, page):
    user, client = owner
    pp = _pp(workspace, user, page, S.SCHEDULED, scheduled_at=timezone.now() + timedelta(days=1))

    response = _move(client, pp, "published")

    pp.refresh_from_db()
    assert pp.status == S.SCHEDULED
    assert _toast(response)["tone"] == "error"


def test_a_contributor_cannot_move_someone_elses_post(workspace, owner, page):
    user, _client = owner
    _contributor, contributor_client = _member(workspace, "c@example.com", Role.CONTRIBUTOR)
    pp = _pp(workspace, user, page, S.DRAFT)

    _move(contributor_client, pp, "review")

    pp.refresh_from_db()
    assert pp.status == S.DRAFT


def test_another_workspaces_post_is_not_found(workspace, owner, page):
    user, client = owner
    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    WorkspaceMembership.objects.create(user=user, workspace=other, workspace_role=Role.OWNER)
    pp = _pp(workspace, user, page, S.DRAFT)

    response = client.post(
        reverse("calendar:status_board_move", kwargs={"workspace_id": other.id, "platform_post_id": pp.id}),
        {"column": "review"},
    )

    assert response.status_code == 404
