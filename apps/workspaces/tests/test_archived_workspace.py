"""An archived workspace accepts no work until it is restored.

Archiving promises that no new posts can be created. That has to hold where a
workspace is addressed directly — its pages, its API keys and the publisher —
not only in the listings that hide it.
"""

import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.api_keys import services as key_services
from apps.composer.models import PlatformPost, Post
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.notifications.models import EventType, Notification
from apps.organizations.models import Organization
from apps.publisher.engine import PublishEngine
from apps.social_accounts.error_messages import PUBLISH_WORKSPACE_ARCHIVED_MESSAGE
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import PublishError


class ArchivedWorkspaceTest(TestCase):
    def setUp(self):
        cache.delete("agent_api:auth_fail:127.0.0.1")
        self.owner = User.objects.create_user(
            email="owner@example.com", password="pw-123456789", name="Owner", tos_accepted_at=timezone.now()
        )
        self.org = Organization.objects.create(name="Org")
        OrgMembership.objects.create(user=self.owner, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        self.active = Workspace.objects.create(name="Active", organization=self.org)
        self.archived = Workspace.objects.create(name="Archived", organization=self.org)
        self.accounts = {}
        for workspace in (self.active, self.archived):
            WorkspaceMembership.objects.create(
                user=self.owner, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
            )
            self.accounts[workspace.id] = SocialAccount.objects.create(
                workspace=workspace,
                platform="facebook",
                account_platform_id=f"page-{workspace.name}",
                account_name="Page",
                connection_status=SocialAccount.ConnectionStatus.CONNECTED,
            )
        self.keys = {
            workspace.id: key_services.issue_api_key(
                workspace=workspace,
                social_accounts=[self.accounts[workspace.id]],
                issued_by=self.owner,
                name="key",
                permissions=list(PERMISSION_KEYS),
            )
            for workspace in (self.active, self.archived)
        }
        self.client.force_login(self.owner)
        response = self.client.post(self._settings_url(self.archived), {"action": "archive_workspace"})
        assert response.status_code == 302
        self.archived.refresh_from_db()
        assert self.archived.is_archived

    @staticmethod
    def _settings_url(workspace):
        return reverse("workspaces:settings", kwargs={"workspace_id": workspace.id})

    def _api_create_post(self, workspace):
        return self.client.post(
            "/api/v1/posts/",
            data=json.dumps(
                {
                    "social_account_id": str(self.accounts[workspace.id].id),
                    "caption": "created through the API",
                    "action": "schedule",
                    "scheduled_at": (timezone.now() + timedelta(days=1)).isoformat(),
                }
            ),
            content_type="application/json",
            secure=True,
            HTTP_AUTHORIZATION=f"Bearer {self.keys[workspace.id].plaintext_token}",
        )

    def _publishing_post(self, workspace):
        post = Post.objects.create(workspace=workspace, caption="queued before the archive", author=self.owner)
        return PlatformPost.objects.create(
            post=post,
            social_account=self.accounts[workspace.id],
            status=PlatformPost.Status.PUBLISHING,
            scheduled_at=timezone.now() - timedelta(minutes=1),
        )

    # -- pages ---------------------------------------------------------

    def test_pages_of_an_archived_workspace_are_refused(self):
        compose = self.client.get(reverse("composer:compose", kwargs={"workspace_id": self.archived.id}))
        dashboard = self.client.get(reverse("workspaces:dashboard", kwargs={"workspace_id": self.archived.id}))

        assert compose.status_code == 403
        assert dashboard.status_code == 403

    def test_pages_of_an_active_workspace_still_open(self):
        response = self.client.get(reverse("composer:compose", kwargs={"workspace_id": self.active.id}))

        assert response.status_code == 200

    def test_the_settings_page_stays_open_and_restores_the_workspace(self):
        assert self.client.get(self._settings_url(self.archived)).status_code == 200

        response = self.client.post(self._settings_url(self.archived), {"action": "unarchive_workspace"})

        assert response.status_code == 302
        self.archived.refresh_from_db()
        assert not self.archived.is_archived
        compose = self.client.get(reverse("composer:compose", kwargs={"workspace_id": self.archived.id}))
        assert compose.status_code == 200

    # -- API keys ------------------------------------------------------

    def test_an_api_key_of_an_archived_workspace_is_refused(self):
        response = self._api_create_post(self.archived)

        assert response.status_code == 401
        assert not Post.objects.filter(workspace=self.archived).exists()

    def test_an_api_key_of_an_active_workspace_still_creates_posts(self):
        response = self._api_create_post(self.active)

        assert response.status_code in (200, 201)
        assert Post.objects.filter(workspace=self.active).count() == 1

    def test_the_api_key_works_again_once_the_workspace_is_restored(self):
        assert self._api_create_post(self.archived).status_code == 401
        self.client.post(self._settings_url(self.archived), {"action": "unarchive_workspace"})

        assert self._api_create_post(self.archived).status_code in (200, 201)

    # -- publisher -----------------------------------------------------

    def test_a_due_post_of_an_archived_workspace_fails_visibly_without_reaching_the_platform(self):
        platform_post = self._publishing_post(self.archived)

        with mock.patch("apps.publisher.engine._provider_and_access_token") as reach_platform:
            result = PublishEngine()._publish_platform_post(platform_post)

        reach_platform.assert_not_called()
        assert result["success"] is False
        platform_post.refresh_from_db()
        assert platform_post.status == PlatformPost.Status.FAILED
        assert platform_post.publish_error == PUBLISH_WORKSPACE_ARCHIVED_MESSAGE
        assert platform_post.can_transition_to(PlatformPost.Status.SCHEDULED)
        assert Notification.objects.filter(user=self.owner, event_type=EventType.POST_FAILED).count() == 1

    def test_a_due_post_of_an_active_workspace_still_reaches_the_platform(self):
        platform_post = self._publishing_post(self.active)
        refused = PublishError("The platform said no.", platform="Facebook", retryable=False)

        with mock.patch("apps.publisher.engine._provider_and_access_token", side_effect=refused) as reach_platform:
            PublishEngine()._publish_platform_post(platform_post)

        reach_platform.assert_called_once()
        platform_post.refresh_from_db()
        assert platform_post.publish_error != PUBLISH_WORKSPACE_ARCHIVED_MESSAGE
