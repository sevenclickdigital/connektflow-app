"""The Templates tab's cards: text versions, a brand's favorites, and the
popular ones counted from real use."""

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone, translation

from apps.composer.models import Idea, IdeaTemplate, IdeaTemplateFavorite, IdeaTemplateUse, IdeaTemplateVariation
from apps.composer.services import POPULAR_TEMPLATES, popular_template_ids, template_shelf
from apps.composer.tests.test_idea_templates import _brand, _operator


def _template(**fields):
    defaults = {"title": "Lista de dicas", "description": "Dicas práticas.", "body": "Versão base", "category": "tip"}
    return IdeaTemplate.objects.create(**{**defaults, **fields})


def _card(workspace, template):
    return next(card for card in template_shelf(workspace) if card["id"] == template.id)


@pytest.mark.django_db
def test_the_shelf_shows_versions_and_characteristics():
    _user, workspace = _brand()
    template = _template(formats=["carousel", "reels", "nope"], goals=["engagement"], networks=["instagram"])
    IdeaTemplateVariation.objects.create(template=template, title="Curta", body="Versão curta", position=0)
    _template(title="Escondido", is_active=False)

    with translation.override("en"):
        shelf = {card["id"]: card for card in template_shelf(workspace)}
    card = shelf[template.id]
    assert not any(other["title"] == "Escondido" for other in shelf.values())

    assert card["variations"] == [{"title": "", "body": "Versão base"}, {"title": "Curta", "body": "Versão curta"}]
    # Labels come translated; an unknown key is left out.
    assert card["formats"] == ["Carousel", "Reels"]
    assert card["networks"] == ["Instagram"]
    assert card["favorite"] is False and card["popular"] is False


@pytest.mark.django_db
def test_a_brand_saves_and_drops_a_favorite_of_its_own(client):
    user, workspace = _brand()
    _other_user, other = _brand("outra@example.com")
    client.force_login(user)
    template = _template()
    url = reverse(
        "composer:template_favorite_toggle", kwargs={"workspace_id": workspace.id, "template_id": template.id}
    )

    assert client.post(url).json() == {"favorite": True}
    assert _card(workspace, template)["favorite"] is True
    assert _card(other, template)["favorite"] is False
    assert client.post(url).json() == {"favorite": False}
    assert not IdeaTemplateFavorite.objects.exists()


@pytest.mark.django_db
def test_a_chosen_version_opens_in_the_composer_and_lands_on_the_board(client):
    user, workspace = _brand()
    client.force_login(user)
    template = _template()
    IdeaTemplateVariation.objects.create(template=template, title="Curta", body="Versão curta")

    compose = client.get(
        reverse("composer:compose", kwargs={"workspace_id": workspace.id}), {"template": template.id, "variation": 1}
    )
    assert compose.status_code == 200
    assert "Versão curta" in compose.content.decode()

    client.post(
        reverse("composer:idea_from_template", kwargs={"workspace_id": workspace.id, "template_id": template.id}),
        {"variation": "1"},
    )
    assert Idea.objects.get(workspace=workspace, title=template.title).description == "Versão curta"
    # Both uses are counted.
    assert IdeaTemplateUse.objects.filter(workspace=workspace, template=template).count() == 2


@pytest.mark.django_db
def test_the_popular_ones_are_the_most_used_lately():
    _user, workspace = _brand()
    templates = [_template(title=f"T{index}") for index in range(POPULAR_TEMPLATES + 2)]
    for uses, template in zip([5, 4, 3, 2], templates, strict=False):
        for _ in range(uses):
            IdeaTemplateUse.objects.create(workspace=workspace, template=template)
    # Old uses do not count.
    old = IdeaTemplateUse.objects.create(workspace=workspace, template=templates[-1])
    IdeaTemplateUse.objects.filter(id=old.id).update(created_at=timezone.now() - dt.timedelta(days=200))

    assert popular_template_ids() == {template.id for template in templates[:POPULAR_TEMPLATES]}


@pytest.mark.django_db
def test_the_operator_fills_characteristics_and_versions(client):
    _operator(client)
    client.post(
        reverse("composer_staff:idea_template_create"),
        {
            "title": "Antes e depois",
            "body": "Base",
            "category": "story",
            "formats": ["post", "carousel"],
            "goals": ["sales"],
            "targets": ["instagram:default", "instagram:reel", "tiktok:default", "tiktok:reel", "nope:default"],
            "ideal_for": "Reformas",
            "is_active": "on",
            "variations-TOTAL_FORMS": "1",
            "variations-INITIAL_FORMS": "0",
            "variations-0-title": "Com depoimento",
            "variations-0-body": "Outra versão",
        },
    )

    template = IdeaTemplate.objects.get(title="Antes e depois")
    assert (template.formats, template.goals, template.networks) == (
        ["post", "carousel"],
        ["sales"],
        ["instagram", "tiktok"],
    )
    # A format a network does not offer (TikTok has no Reels) and an unknown network are dropped.
    assert template.network_formats == {"instagram": ["default", "reel"], "tiktok": ["default"]}
    assert template.ideal_for == "Reformas"
    assert list(template.variations.values_list("title", "body")) == [("Com depoimento", "Outra versão")]


@pytest.mark.django_db
def test_a_template_opens_with_the_accounts_of_its_networks_ticked(client):
    from apps.social_accounts.models import SocialAccount

    user, workspace = _brand()
    client.force_login(user)
    template = _template(
        networks=["instagram", "linkedin"], network_formats={"instagram": ["reel"], "linkedin": ["default"]}
    )

    def account(platform):
        return SocialAccount.objects.create(
            workspace=workspace,
            platform=platform,
            account_platform_id=f"{platform}-1",
            account_name=platform,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    instagram, linkedin, facebook = account("instagram"), account("linkedin_company"), account("facebook")

    response = client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id}), {"template": template.id})

    selected = set(response.context["selected_account_ids"])
    assert selected == {str(instagram.id), str(linkedin.id)}
    assert str(facebook.id) not in selected
    # Each account on the formats the template names for its network.
    assert response.context["selected_formats"] == {str(instagram.id): ["reel"], str(linkedin.id): ["default"]}
