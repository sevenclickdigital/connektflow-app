"""Templates sent by customers: hidden until the operator approves them,
with the versions of the text they brought."""

import json

import pytest
from django.urls import reverse

from apps.composer.models import Idea, IdeaTemplate
from apps.composer.services import MAX_PENDING_TEMPLATE_SUBMISSIONS, template_shelf
from apps.composer.tests.test_idea_templates import _operator
from apps.composer.tests.test_tag_manager import _brand
from apps.members.models import WorkspaceMembership


def _submit(client, workspace, **extra):
    data = {"title": "3 erros dos meus clientes", "category": "tip", "description": "", "body": "Erro 1: {{erro}}"}
    return client.post(
        reverse("composer:template_submit", kwargs={"workspace_id": workspace.id}),
        {**data, **extra},
        HTTP_HX_REQUEST="true",
    )


@pytest.mark.django_db
def test_a_sent_template_waits_hidden_with_its_versions(client):
    user, workspace = _brand()
    client.force_login(user)

    response = _submit(client, workspace, variation_title=["Curta", "Vazia"], variation_body=["Erro: {{erro}}", "   "])

    assert response.status_code == 204
    assert json.loads(response["HX-Trigger"])["showToast"]["tone"] == "success"
    template = IdeaTemplate.objects.get(title="3 erros dos meus clientes")
    assert (template.is_active, template.review_status) == (False, IdeaTemplate.Review.PENDING)
    assert (template.submitted_by, template.submitted_workspace) == (user, workspace)
    assert list(template.variations.values_list("title", "body")) == [("Curta", "Erro: {{erro}}")]
    # Hidden from every other brand until approved.
    _other_user, other = _brand("terceira@example.com")
    assert all(card["id"] != template.id for card in template_shelf(other))


@pytest.mark.django_db
def test_a_sent_template_brings_the_same_fields_as_the_operators(client):
    user, workspace = _brand()
    client.force_login(user)

    _submit(
        client,
        workspace,
        icon="megaphone",
        details="Quando usar",
        formats=["reels"],
        goals=["sales"],
        targets=["tiktok:default"],
        ideal_for="Lançamentos",
    )
    _submit(client, workspace, title="Ícone inválido", icon="nao-existe")

    template = IdeaTemplate.objects.get(title="3 erros dos meus clientes")
    assert (template.details, template.formats, template.goals, template.networks, template.ideal_for) == (
        "Quando usar",
        ["reels"],
        ["sales"],
        ["tiktok"],
        "Lançamentos",
    )
    assert template.icon == "megaphone"
    assert IdeaTemplate.objects.get(title="Ícone inválido").icon == ""


@pytest.mark.django_db
def test_a_brand_cannot_flood_the_queue_nor_send_without_text(client):
    user, workspace = _brand()
    client.force_login(user)

    assert json.loads(_submit(client, workspace, body=" ")["HX-Trigger"])["showToast"]["tone"] == "error"
    for _ in range(MAX_PENDING_TEMPLATE_SUBMISSIONS):
        _submit(client, workspace)
    refused = _submit(client, workspace)

    assert json.loads(refused["HX-Trigger"])["showToast"]["tone"] == "error"
    assert IdeaTemplate.objects.filter(submitted_workspace=workspace).count() == MAX_PENDING_TEMPLATE_SUBMISSIONS


@pytest.mark.django_db
def test_a_viewer_cannot_send_templates(client):
    user, workspace = _brand(role=WorkspaceMembership.WorkspaceRole.VIEWER)
    client.force_login(user)

    assert _submit(client, workspace).status_code == 403
    assert not IdeaTemplate.objects.filter(submitted_workspace=workspace).exists()


@pytest.mark.django_db
def test_the_operator_approves_or_rejects_what_was_sent(client):
    user, workspace = _brand()
    client.force_login(user)
    _submit(client, workspace, title="Aprovar")
    _submit(client, workspace, title="Recusar")
    approve = IdeaTemplate.objects.get(title="Aprovar")
    reject = IdeaTemplate.objects.get(title="Recusar")

    _operator(client)
    sent = client.get(reverse("composer_staff:idea_templates"), {"tab": "sent"})
    assert {item.id for item in sent.context["templates"]} == {approve.id, reject.id}

    def review(template, decision):
        client.post(
            reverse("composer_staff:idea_template_review", kwargs={"template_id": template.id}), {"decision": decision}
        )

    review(approve, "approve")
    review(reject, "reject")

    approve.refresh_from_db()
    reject.refresh_from_db()
    assert (approve.is_active, approve.review_status) == (True, IdeaTemplate.Review.APPROVED)
    assert (reject.is_active, reject.review_status) == (False, IdeaTemplate.Review.REJECTED)
    assert any(card["id"] == approve.id for card in template_shelf(workspace))


@pytest.mark.django_db
def test_a_brand_uses_its_own_sent_templates_at_once_and_others_do_not_see_them(client):
    user, workspace = _brand()
    other_user, other = _brand("outra@example.com")
    client.force_login(user)
    _submit(client, workspace, title="Só meu")
    mine = IdeaTemplate.objects.get(title="Só meu")

    card = next(card for card in template_shelf(workspace) if card["id"] == mine.id)
    assert (card["mine"], card["review"]) == (True, IdeaTemplate.Review.PENDING)
    compose = client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id}), {"template": mine.id})
    assert "Erro 1: {{erro}}" in compose.content.decode()
    client.post(reverse("composer:idea_from_template", kwargs={"workspace_id": workspace.id, "template_id": mine.id}))
    assert Idea.objects.filter(workspace=workspace, title="Só meu").exists()

    assert all(card["id"] != mine.id for card in template_shelf(other))
    client.force_login(other_user)
    refused = client.post(
        reverse("composer:idea_from_template", kwargs={"workspace_id": other.id, "template_id": mine.id})
    )
    assert refused.status_code == 404
