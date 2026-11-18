"""The composer's two edit rules, on the REST posts routes.

* Someone else's post needs ``edit_others_posts`` to be edited, unscheduled or
  scheduled — the key's issuer is the acting user.
* Changing the reviewable content of an approved post sends it back to review.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def workspace(db):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="WS", organization=Organization.objects.create(name="Edit Rules Org"))


def _member(workspace, email):
    from apps.accounts.models import User

    member = User.objects.create_user(email=email, password="testpass123", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=member, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=member, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return member


@pytest.fixture
def issuer(workspace):
    return _member(workspace, "edit-rules-issuer@example.com")


@pytest.fixture
def colleague(workspace):
    return _member(workspace, "edit-rules-colleague@example.com")


@pytest.fixture
def social_account(workspace):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="li-edit-rules",
        account_name="LinkedIn Edit Rules",
        connection_status="connected",
    )


def _client(workspace, issuer, social_account, permissions):
    issued = services.issue_api_key(
        workspace=workspace,
        social_accounts=[social_account],
        issued_by=issuer,
        name="edit rules",
        permissions=permissions,
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {issued.plaintext_token}")


def _post(workspace, author, social_account, status, **fields):
    post = Post.objects.create(workspace=workspace, author=author, caption="reviewed text", **fields)
    scheduled_at = timezone.now() + timedelta(days=1) if status == "scheduled" else None
    PlatformPost.objects.create(post=post, social_account=social_account, status=status, scheduled_at=scheduled_at)
    return post


def _patch(client, post, body):
    return client.patch(f"/api/v1/posts/{post.id}", data=json.dumps(body), content_type="application/json")


def _status(post):
    return post.platform_posts.get().status


def _image(workspace, name):
    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        filename=name,
        media_type=MediaAsset.MediaType.IMAGE,
        file_size=1,
        processing_status=MediaAsset.ProcessingStatus.COMPLETED,
    )


@pytest.mark.django_db
class TestApprovedPostGoesBackToReview:
    def test_caption_change_reverts_approval(self, workspace, issuer, social_account):
        client = _client(workspace, issuer, social_account, ["create_posts"])
        post = _post(workspace, issuer, social_account, "approved")

        response = _patch(client, post, {"caption": "text nobody reviewed"})

        assert response.status_code == 200
        post.refresh_from_db()
        assert post.caption == "text nobody reviewed"
        assert _status(post) == "pending_review"

    def test_media_change_reverts_approval(self, workspace, issuer, social_account):
        client = _client(workspace, issuer, social_account, ["create_posts"])
        post = _post(workspace, issuer, social_account, "approved")
        PostMedia.objects.create(post=post, media_asset=_image(workspace, "reviewed.png"), position=0)
        swapped = _image(workspace, "swapped.png")

        response = _patch(client, post, {"media_asset_ids": [str(swapped.id)]})

        assert response.status_code == 200
        assert _status(post) == "pending_review"

    def test_unchanged_content_keeps_approval(self, workspace, issuer, social_account):
        client = _client(workspace, issuer, social_account, ["create_posts"])
        post = _post(workspace, issuer, social_account, "approved")
        asset = _image(workspace, "reviewed.png")
        PostMedia.objects.create(post=post, media_asset=asset, position=0)

        response = _patch(
            client,
            post,
            {"caption": "reviewed text", "internal_notes": "team only", "media_asset_ids": [str(asset.id)]},
        )

        assert response.status_code == 200
        assert _status(post) == "approved"


@pytest.mark.django_db
class TestOthersPostsNeedEditOthersPosts:
    def test_patch_refused_and_post_unchanged(self, workspace, issuer, colleague, social_account):
        client = _client(workspace, issuer, social_account, ["create_posts"])
        post = _post(workspace, colleague, social_account, "approved")

        response = _patch(client, post, {"caption": "rewritten by someone else"})

        assert response.status_code == 403
        post.refresh_from_db()
        assert post.caption == "reviewed text"
        assert _status(post) == "approved"

    def test_cancel_refused_and_post_stays_scheduled(self, workspace, issuer, colleague, social_account):
        client = _client(workspace, issuer, social_account, ["create_posts"])
        post = _post(workspace, colleague, social_account, "scheduled")

        response = client.post(f"/api/v1/posts/{post.id}/cancel")

        assert response.status_code == 403
        assert _status(post) == "scheduled"

    def test_schedule_refused_and_post_stays_draft(self, workspace, issuer, colleague, social_account):
        client = _client(workspace, issuer, social_account, ["create_posts", "publish_directly"])
        post = _post(workspace, colleague, social_account, "draft")

        response = client.post(
            f"/api/v1/posts/{post.id}/schedule",
            data=json.dumps({"scheduled_at": (timezone.now() + timedelta(days=1)).isoformat()}),
            content_type="application/json",
        )

        assert response.status_code == 403
        assert _status(post) == "draft"

    def test_own_post_can_be_cancelled(self, workspace, issuer, social_account):
        client = _client(workspace, issuer, social_account, ["create_posts"])
        post = _post(workspace, issuer, social_account, "scheduled")

        response = client.post(f"/api/v1/posts/{post.id}/cancel")

        assert response.status_code == 200
        assert _status(post) == "draft"

    def test_key_with_edit_others_posts_can_edit_and_cancel(self, workspace, issuer, colleague, social_account):
        client = _client(workspace, issuer, social_account, ["create_posts", "edit_others_posts"])
        post = _post(workspace, colleague, social_account, "scheduled")

        assert _patch(client, post, {"caption": "fixed a typo"}).status_code == 200
        assert client.post(f"/api/v1/posts/{post.id}/cancel").status_code == 200

        post.refresh_from_db()
        assert post.caption == "fixed a typo"
        assert _status(post) == "draft"


@pytest.mark.django_db
class TestScheduledPostEditedByAKeyThatNeedsApproval:
    """A scheduled row got there on a reviewer's word for the content as it was."""

    def _require_approval(self, workspace):
        workspace.approval_workflow_mode = "required_internal"
        workspace.save(update_fields=["approval_workflow_mode"])

    def test_caption_change_takes_the_row_out_of_the_publish_path(self, workspace, issuer, social_account):
        self._require_approval(workspace)
        client = _client(workspace, issuer, social_account, ["create_posts"])
        post = _post(workspace, issuer, social_account, "scheduled")

        response = _patch(client, post, {"caption": "text nobody reviewed"})

        assert response.status_code == 200
        assert _status(post) == "pending_review"

    def test_a_key_that_may_publish_directly_keeps_the_row_scheduled(self, workspace, issuer, social_account):
        self._require_approval(workspace)
        client = _client(workspace, issuer, social_account, ["create_posts", "publish_directly"])
        post = _post(workspace, issuer, social_account, "scheduled")

        response = _patch(client, post, {"caption": "the publisher's own edit"})

        assert response.status_code == 200
        assert _status(post) == "scheduled"
