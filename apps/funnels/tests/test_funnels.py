"""Funnels over inbox conversations: screens, opportunity edits, board and settings."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.funnels import services
from apps.funnels.models import Funnel, FunnelStage, Opportunity, OpportunityActivity
from apps.inbox.models import InboxMessage
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="Funnel WS", organization=organization)


@pytest.fixture
def account(workspace):
    return SocialAccount.objects.create(
        workspace=workspace, platform="facebook", account_platform_id="page-1", account_name="Page"
    )


@pytest.fixture
def member(client, workspace, org_owner, user):
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    client.force_login(user)
    return client


def _message(workspace, account, pid, *, sender="ana", hours_ago=1, message_type="dm", **fields):
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id=pid,
        message_type=message_type,
        sender_name=sender.title(),
        sender_handle=sender,
        body=f"body {pid}",
        received_at=timezone.now() - timedelta(hours=hours_ago),
        **fields,
    )


def _stages(workspace):
    funnel = services.funnels_for(workspace)[0]
    return funnel, list(funnel.stages.all())


@pytest.mark.django_db
class TestFunnelScreen:
    def test_every_conversation_starts_in_the_main_funnels_first_stage(self, member, workspace, account):
        _message(workspace, account, "a1", sender="ana")
        _message(workspace, account, "b1", sender="bia")
        funnel, stages = _stages(workspace)

        response = member.get(reverse("funnels:inbox", kwargs={"workspace_id": workspace.id}), HTTP_HX_REQUEST="true")

        assert funnel.is_default and len(stages) == 5
        assert response.context["total"] == 2
        assert dict((str(s.id), n) for s, n in response.context["stages"])[str(stages[0].id)] == 2
        assert {m.stage.id for m in response.context["inbox_messages"]} == {stages[0].id}

    def test_a_stage_filters_the_list(self, member, workspace, account):
        moved = _message(workspace, account, "a1", sender="ana")
        _message(workspace, account, "b1", sender="bia")
        funnel, stages = _stages(workspace)
        services.move(services.ensure_opportunity(moved, [funnel]), stages[2])

        response = member.get(
            reverse("funnels:inbox", kwargs={"workspace_id": workspace.id}),
            {"stage": str(stages[2].id)},
            HTTP_HX_REQUEST="true",
        )

        assert [m.id for m in response.context["inbox_messages"]] == [moved.id]

    def test_the_entry_opens_with_its_opportunity(self, member, workspace, account):
        message = _message(workspace, account, "a1")
        url = reverse("inbox:message_detail", kwargs={"workspace_id": workspace.id, "message_id": message.id})

        body = member.get(url, {"mode": "funnel"}, HTTP_HX_REQUEST="true").content.decode()

        assert 'id="opportunity-title"' in body
        assert 'hx-vals=\'{"mode": "funnel"}\'' in body


@pytest.mark.django_db
class TestOpportunity:
    def _save(self, client, workspace, message, **data):
        url = reverse("funnels:opportunity_update", kwargs={"workspace_id": workspace.id, "message_id": message.id})
        return client.post(url, data)

    def test_choosing_a_stage_writes_the_opportunity_and_its_activity(self, member, workspace, account, user):
        message = _message(workspace, account, "a1")
        later = _message(workspace, account, "a2", hours_ago=0)
        _, stages = _stages(workspace)

        response = self._save(member, workspace, message, field="stage", value=str(stages[1].id))

        assert response.status_code == 200
        opportunity = Opportunity.objects.get()
        assert opportunity.stage == stages[1] and opportunity.name == "Ana"
        assert OpportunityActivity.objects.get().actor == user
        # The conversation shares it: its other messages read the same stage.
        assert services.thread_opportunity(later) == opportunity

    def test_value_tags_interest_and_notes(self, member, workspace, account):
        message = _message(workspace, account, "a1")
        self._save(member, workspace, message, field="value", value="1.500,50")
        self._save(member, workspace, message, field="add_tag", value="Delivery")
        self._save(member, workspace, message, field="add_tag", value="delivery")
        self._save(member, workspace, message, field="interest", value="Cardápio digital")
        self._save(member, workspace, message, field="notes", value="Ligar sexta")

        opportunity = Opportunity.objects.get()
        assert opportunity.value == Decimal("1500.50")
        assert opportunity.tags == ["Delivery"]
        assert (opportunity.interest, opportunity.notes) == ("Cardápio digital", "Ligar sexta")

    def test_a_value_that_is_not_a_number_is_refused(self, member, workspace, account):
        message = _message(workspace, account, "a1")

        response = self._save(member, workspace, message, field="value", value="muito")

        assert response.status_code == 204
        assert "showToast" in response["HX-Trigger"]
        assert not Opportunity.objects.exists()

    def test_next_stage(self, member, workspace, account):
        message = _message(workspace, account, "a1")
        _, stages = _stages(workspace)
        url = reverse("funnels:opportunity_next", kwargs={"workspace_id": workspace.id, "message_id": message.id})

        member.post(url)

        assert Opportunity.objects.get().stage == stages[1]

    def test_another_workspaces_stage_is_not_found(self, member, workspace, account, organization):
        message = _message(workspace, account, "a1")
        other = Workspace.objects.create(name="Other", organization=organization)
        foreign_stage = services.funnels_for(other)[0].stages.first()

        response = self._save(member, workspace, message, field="stage", value=str(foreign_stage.id))

        assert response.status_code == 404
        assert not Opportunity.objects.exists()

    def test_another_workspaces_conversation_is_not_found(self, member, workspace, organization):
        other = Workspace.objects.create(name="Other", organization=organization)
        other_account = SocialAccount.objects.create(workspace=other, platform="facebook", account_platform_id="x")
        foreign = _message(other, other_account, "x1")

        response = self._save(member, workspace, foreign, field="notes", value="x")

        assert response.status_code == 404

    def test_a_viewer_cannot_edit(self, client, workspace, account, organization):
        from apps.accounts.models import User

        viewer = User.objects.create_user(email="v@example.com", password="x", tos_accepted_at=timezone.now())
        WorkspaceMembership.objects.create(
            user=viewer, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )
        client.force_login(viewer)
        message = _message(workspace, account, "a1")

        response = self._save(client, workspace, message, field="notes", value="x")

        assert response.status_code == 403


@pytest.mark.django_db
class TestBoard:
    def test_columns_hold_the_conversations_with_their_value(self, member, workspace, account):
        message = _message(workspace, account, "a1")
        _message(workspace, account, "b1", sender="bia")
        funnel, stages = _stages(workspace)
        opportunity = services.ensure_opportunity(message, [funnel])
        opportunity.value = Decimal("900")
        opportunity.save()
        services.move(opportunity, stages[-1])

        response = member.get(reverse("funnels:board", kwargs={"workspace_id": workspace.id}))

        columns = {str(c["stage"].id): c for c in response.context["columns"]}
        assert columns[str(stages[0].id)]["count"] == 1
        assert columns[str(stages[-1].id)]["value"] == Decimal("900")
        assert response.context["figures"]["conversion"] == 50.0

    def test_dropping_a_card_moves_it(self, member, workspace, account):
        message = _message(workspace, account, "a1")
        _, stages = _stages(workspace)

        response = member.post(
            reverse("funnels:board_move", kwargs={"workspace_id": workspace.id}),
            {"message": str(message.id), "stage": str(stages[3].id)},
        )

        assert response.status_code == 204
        assert "board-refresh" in response["HX-Trigger"]
        assert Opportunity.objects.get().stage == stages[3]


@pytest.mark.django_db
class TestSettings:
    def test_a_new_funnel_starts_with_a_stage(self, member, workspace):
        services.funnels_for(workspace)

        member.post(reverse("funnels:funnel_save", kwargs={"workspace_id": workspace.id}), {"name": "Suporte"})

        funnel = Funnel.objects.get(name="Suporte")
        assert not funnel.is_default and funnel.stages.count() == 1

    def test_deleting_a_stage_moves_its_conversations(self, member, workspace, account):
        message = _message(workspace, account, "a1")
        funnel, stages = _stages(workspace)
        services.move(services.ensure_opportunity(message, [funnel]), stages[2])

        member.post(reverse("funnels:stage_delete", kwargs={"workspace_id": workspace.id, "stage_id": stages[2].id}))

        assert not FunnelStage.objects.filter(id=stages[2].id).exists()
        assert Opportunity.objects.get().stage == stages[0]

    def test_the_main_funnel_stays(self, member, workspace):
        funnel, _ = _stages(workspace)

        member.post(reverse("funnels:funnel_delete", kwargs={"workspace_id": workspace.id, "funnel_id": funnel.id}))

        assert Funnel.objects.filter(id=funnel.id).exists()

    def test_reordering_and_adding_stages(self, member, workspace):
        funnel, stages = _stages(workspace)

        member.post(
            reverse("funnels:stage_reorder", kwargs={"workspace_id": workspace.id, "stage_id": stages[1].id}),
            {"direction": "up"},
        )
        member.post(
            reverse("funnels:stage_save", kwargs={"workspace_id": workspace.id, "funnel_id": funnel.id}),
            {"name": "Perdido", "color": "red", "kind": "lost"},
        )

        names = list(funnel.stages.values_list("name", "kind"))
        assert names[0][0] == stages[1].name and names[-1] == ("Perdido", "lost")


@pytest.mark.django_db
class TestFlowStart:
    def test_a_conversation_without_a_flow_has_none_to_start(self, member, workspace, account):
        message = _message(workspace, account, "a1")
        url = reverse("funnels:flow_start", kwargs={"workspace_id": workspace.id, "message_id": message.id})

        assert member.post(url, {"flow": "00000000-0000-0000-0000-000000000000"}).status_code == 404

    def test_an_old_comment_is_refused_before_anything_is_claimed(self, workspace, account):
        from apps.automations.actions import start_flow_for_message
        from apps.automations.models import AutomationLog

        message = _message(workspace, account, "c1", message_type="comment", hours_ago=24 * 9)
        rule = mock.Mock(once_per_sender_per_post=True)

        sent, reason = start_flow_for_message(message, rule)

        assert not sent and "7" in reason
        assert not AutomationLog.objects.exists()
