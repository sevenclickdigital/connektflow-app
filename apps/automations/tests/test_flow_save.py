"""What ``flow_save`` stores of a posted canvas.

The editor writes each node's position into the SVG markup of the connecting
lines, so a position reaches the page of every member who opens the flow. These
pin that only numbers are stored, whatever the client posts.
"""

from __future__ import annotations

import json

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.automations import flow
from apps.automations.models import PostAutomation
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

MARKUP = '"></path><g x-init="alert(document.domain)"></g><path d="'


class SavedPositionsTests(TestCase):
    def setUp(self):
        self.editor = User.objects.create_user(
            email="editor@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=org, name="WS")
        OrgMembership.objects.create(user=self.editor, organization=org, org_role=OrgMembership.OrgRole.MEMBER)
        WorkspaceMembership.objects.create(
            user=self.editor,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR,
        )
        account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="fb-1",
            account_name="Page",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        post = Post.objects.create(workspace=self.workspace, caption="hello")
        self.platform_post = PlatformPost.objects.create(
            post=post, social_account=account, status=PlatformPost.Status.DRAFT
        )
        self.client.force_login(self.editor)

    def _save(self, nodes):
        url = reverse(
            "automations:flow_save",
            kwargs={"workspace_id": self.workspace.id, "platform_post_id": self.platform_post.id},
        )
        graph = {"nodes": nodes, "edges": [{"from": "a", "to": "b"}]}
        return self.client.post(url, {"graph": json.dumps(graph)})

    def test_a_position_that_is_not_a_number_is_stored_as_zero(self):
        response = self._save(
            [
                {"id": "a", "type": "trigger_comment", "x": MARKUP, "y": None, "data": {}},
                {"id": "b", "type": "send_dm", "x": [1], "data": {"text": "hi"}},
            ]
        )

        self.assertEqual(response.status_code, 200)
        nodes = PostAutomation.objects.get(platform_post=self.platform_post).graph["nodes"]
        self.assertEqual([(n["x"], n["y"]) for n in nodes], [(0, 0), (0, 0)])
        self.assertNotIn("x-init", json.dumps(nodes))

    def test_numeric_positions_and_the_rest_of_the_node_are_kept(self):
        response = self._save(
            [
                {"id": "a", "type": "trigger_comment", "x": 40, "y": -12.5, "data": {}},
                {"id": "b", "type": "send_dm", "x": 400, "y": 80, "data": {"text": "hi"}},
            ]
        )

        self.assertEqual(response.status_code, 200)
        nodes = PostAutomation.objects.get(platform_post=self.platform_post).graph["nodes"]
        self.assertEqual([(n["x"], n["y"]) for n in nodes], [(40, -12.5), (400, 80)])
        self.assertEqual(nodes[1]["data"], {"text": "hi"})


class NumericPositionsTests(SimpleTestCase):
    def test_values_json_can_carry_but_a_canvas_cannot_draw(self):
        nodes = flow.numeric_positions(
            [
                {"id": "a", "x": True, "y": float("nan")},
                {"id": "b", "x": float("inf"), "y": 10**400},
                "not a node",
            ]
        )

        self.assertEqual(nodes[0], {"id": "a", "x": 0, "y": 0})
        self.assertEqual(nodes[1], {"id": "b", "x": 0, "y": 0})
        self.assertEqual(nodes[2], "not a node")
