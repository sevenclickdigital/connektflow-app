"""The composer asks for a post's automation targets after saving, so the
"set up flow" buttons work without reloading the page."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


def _brand(email):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    org = Organization.objects.create(name=f"Org of {email}")
    workspace = Workspace.objects.create(organization=org, name="Brand")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user, workspace


@pytest.mark.django_db
def test_a_saved_draft_reports_where_its_flow_is_set_up(client):
    user, workspace = _brand("dona@example.com")
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="fb-1",
        account_name="Page",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    client.force_login(user)

    client.post(
        reverse("composer:save_post", kwargs={"workspace_id": workspace.id}),
        {"action": "save_draft", "title": "", "caption": "hello", "tags": "", "selected_accounts": str(account.id)},
    )
    post = Post.objects.get(workspace=workspace)
    platform_post = PlatformPost.objects.get(post=post)

    response = client.get(
        reverse("composer:automation_targets", kwargs={"workspace_id": workspace.id, "post_id": post.id})
    )

    assert response.json() == {
        f"{account.id}:{platform_post.format}": {
            "platform_post_id": str(platform_post.id),
            "status": "",
            "status_label": "",
            "has_flow": False,
        }
    }


@pytest.mark.django_db
def test_the_targets_of_another_brand_are_not_given(client):
    _owner, workspace = _brand("dona@example.com")
    post = Post.objects.create(workspace=workspace, author=_owner, caption="private")
    stranger, own_workspace = _brand("outra@example.com")
    client.force_login(stranger)

    # Through the stranger's own brand, and through the brand it belongs to.
    for workspace_id in (own_workspace.id, workspace.id):
        response = client.get(
            reverse("composer:automation_targets", kwargs={"workspace_id": workspace_id, "post_id": post.id})
        )
        assert response.status_code in (403, 404)
