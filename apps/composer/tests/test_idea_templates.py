"""Idea templates: the shelf every brand sees in the create page, kept by the
operator and read by the composer through ``?template=<id>``."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import IdeaTemplate
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


def _brand(email="dona@example.com"):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    org = Organization.objects.create(name="Org")
    workspace = Workspace.objects.create(organization=org, name="Brand")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user, workspace


#: The management fields of the template form's (empty) list of text versions.
NO_VERSIONS = {"variations-TOTAL_FORMS": "0", "variations-INITIAL_FORMS": "0"}


def _operator(client):
    operator = User.objects.create_user(
        email="op@example.com", password="x", tos_accepted_at=timezone.now(), is_staff=True
    )
    client.force_login(operator)


@pytest.mark.django_db
def test_the_shelf_starts_with_the_templates_that_shipped_and_their_ids():
    from apps.composer.builtin_templates import TEMPLATES

    assert set(IdeaTemplate.objects.values_list("id", flat=True)) == {t["id"] for t in TEMPLATES}
    # The sequence was moved past the ids given by hand.
    assert IdeaTemplate.objects.create(title="Novo").id > max(t["id"] for t in TEMPLATES)


@pytest.mark.django_db
def test_the_create_page_lists_only_the_templates_on_offer(client):
    user, workspace = _brand()
    hidden = IdeaTemplate.objects.create(title="Escondido", is_active=False)
    shown = IdeaTemplate.objects.create(title="Visível")
    client.force_login(user)

    context = client.get(reverse("composer:create_templates", kwargs={"workspace_id": workspace.id})).context

    ids = {t.id for t in context["builtin_templates"]}
    assert shown.id in ids and hidden.id not in ids


@pytest.mark.django_db
def test_the_composer_opens_with_the_text_of_the_template(client):
    user, workspace = _brand()
    template = IdeaTemplate.objects.create(title="Oferta", body="Só hoje: {{oferta}}")
    hidden = IdeaTemplate.objects.create(title="Fora", body="não deve abrir", is_active=False)
    client.force_login(user)
    url = reverse("composer:compose", kwargs={"workspace_id": workspace.id})

    assert "Só hoje: {{oferta}}" in client.get(url, {"template": template.id}).content.decode()
    assert "não deve abrir" not in client.get(url, {"template": hidden.id}).content.decode()


@pytest.mark.django_db
def test_the_operator_creates_edits_hides_and_deletes_a_template(client):
    _operator(client)

    assert client.get(reverse("composer_staff:idea_templates")).status_code == 200
    client.post(
        reverse("composer_staff:idea_template_create"),
        {
            "emoji": "💡",
            "title": "Dica rápida",
            "description": "Uma dica.",
            "body": "Texto",
            "category": "tip",
            "is_active": "on",
            **NO_VERSIONS,
        },
    )
    template = IdeaTemplate.objects.get(title="Dica rápida")

    edit = reverse("composer_staff:idea_template_edit", kwargs={"template_id": template.id})
    client.post(
        edit,
        {
            "emoji": "💡",
            "title": "Dica do dia",
            "description": "",
            "body": "",
            "category": "story",
            "is_active": "on",
            **NO_VERSIONS,
        },
    )
    # A category that does not exist is refused, and nothing changes.
    client.post(edit, {"title": "Quebrado", "category": "nao-existe", **NO_VERSIONS})
    template.refresh_from_db()
    assert (template.title, template.category) == ("Dica do dia", "story")

    client.post(reverse("composer_staff:idea_template_toggle", kwargs={"template_id": template.id}))
    template.refresh_from_db()
    assert template.is_active is False

    client.post(reverse("composer_staff:idea_template_delete", kwargs={"template_id": template.id}))
    assert not IdeaTemplate.objects.filter(id=template.id).exists()


@pytest.mark.django_db
def test_a_customer_cannot_touch_the_shelf(client):
    user, _workspace = _brand()
    template = IdeaTemplate.objects.create(title="Intocado")
    client.force_login(user)

    assert client.get(reverse("composer_staff:idea_templates")).status_code in (302, 403, 404)
    client.post(reverse("composer_staff:idea_template_create"), {"title": "Invasão", "category": "tip"})
    client.post(reverse("composer_staff:idea_template_delete", kwargs={"template_id": template.id}))

    assert not IdeaTemplate.objects.filter(title="Invasão").exists()
    assert IdeaTemplate.objects.filter(id=template.id).exists()


@pytest.mark.django_db
def test_the_brand_sees_the_hashtags_it_publishes_with_most_used_first():
    from datetime import date

    from apps.analytics.models import PostInsightsSnapshot
    from apps.composer.models import PlatformPost, Post
    from apps.composer.services import brand_hashtags
    from apps.social_accounts.models import SocialAccount

    user, workspace = _brand()
    _other_user, other = _brand("outra@example.com")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="instagram", account_platform_id="ig-1", account_name="IG"
    )

    def publish(caption, status="published", first_comment="", brand=workspace):
        post = Post.objects.create(workspace=brand, author=user, caption=caption, first_comment=first_comment)
        return PlatformPost.objects.create(
            post=post, social_account=account, status=status, published_at=timezone.now()
        )

    first = publish("Pão quentinho #Padaria #pão")
    publish("Promoção de hoje #padaria", first_comment="#promo")
    publish("Rascunho #segredo", status="draft")
    publish("De outra marca #alheia", brand=other)
    PostInsightsSnapshot.objects.create(platform_post=first, metric_key="reach", date=date(2026, 11, 1), value=100)
    PostInsightsSnapshot.objects.create(platform_post=first, metric_key="reach", date=date(2026, 11, 2), value=300)

    tags = brand_hashtags(workspace.id)

    assert [(t["tag"], t["posts"]) for t in tags] == [("padaria", 2), ("promo", 1), ("pão", 1)]
    # The latest reach reported, averaged over the publications that have one.
    assert tags[0]["reach"] == 300 and tags[1]["reach"] is None


@pytest.mark.django_db
def test_each_page_of_the_create_area_has_its_own_address(client):
    user, workspace = _brand()
    client.force_login(user)
    kwargs = {"workspace_id": workspace.id}

    for name, tab in (("create_landing", "ideas"), ("create_templates", "templates"), ("create_dates", "dates")):
        response = client.get(reverse(f"composer:{name}", kwargs=kwargs))
        assert response.status_code == 200
        assert response.context["tab"] == tab
    # A link in the old shape goes to the page it meant.
    response = client.get(reverse("composer:create_landing", kwargs=kwargs), {"tab": "dates"})
    assert response.headers["Location"] == reverse("composer:create_dates", kwargs=kwargs)
    # The idea board does not carry the templates.
    assert list(client.get(reverse("composer:create_landing", kwargs=kwargs)).context["builtin_templates"]) == []


@pytest.mark.django_db
def test_a_new_board_sorts_ideas_and_knows_the_ones_in_production(client):
    from apps.composer.models import Idea, IdeaGroup, Post

    user, workspace = _brand()
    client.force_login(user)
    url = reverse("composer:create_landing", kwargs={"workspace_id": workspace.id})

    columns = client.get(url).context["columns"]
    assert [c["label"] for c in columns] == ["Brainstorming", "Em avaliação", "Aprovadas", "Descartadas"]
    assert all(c["hint"] for c in columns)
    # A column that is not the one created there says nothing about itself.
    IdeaGroup.objects.filter(workspace=workspace, position=3).update(name="Pronto")
    assert [bool(c["hint"]) for c in client.get(url).context["columns"]] == [True, True, True, False]
    assert [c["tone"] for c in client.get(url).context["columns"]] == ["brand", "warning", "success", "brand"]
    IdeaGroup.objects.filter(workspace=workspace, position=3).update(name="Descartadas")

    first = IdeaGroup.objects.get(workspace=workspace, position=0)
    waiting = Idea.objects.create(workspace=workspace, group=first, title="Ainda esperando")
    Idea.objects.create(
        workspace=workspace,
        group=first,
        title="Virou publicação",
        post=Post.objects.create(workspace=workspace, author=user, caption="x"),
    )

    response = client.get(url)
    assert waiting.title in response.content.decode()
    # The card no longer shows the mark, but the board still knows which
    # ideas went on to production.
    ideas = {idea.title: idea for idea in response.context["columns"][0]["ideas"]}
    assert ideas["Virou publicação"].post_id and not ideas["Ainda esperando"].post_id
    assert not ideas["Ainda esperando"].has_task


@pytest.mark.django_db
def test_an_idea_keeps_the_icon_picked_for_it(client):
    from apps.composer import idea_icons
    from apps.composer.models import Idea

    user, workspace = _brand()
    client.force_login(user)
    kwargs = {"workspace_id": workspace.id}
    client.get(reverse("composer:create_landing", kwargs=kwargs))  # creates the board

    client.post(reverse("composer:idea_create", kwargs=kwargs), {"title": "Promoção de sábado", "icon": "target"})
    idea = Idea.objects.get(title="Promoção de sábado")
    assert idea.icon == "target"
    page = client.get(reverse("composer:create_landing", kwargs=kwargs)).content.decode()
    assert 'data-icon="target"' in page
    assert idea_icons.ICONS["target"][1] in page

    edit = reverse("composer:idea_edit", kwargs={**kwargs, "idea_id": idea.id})
    # Not an icon of the library: dropped, never stored or printed on the card.
    client.post(edit, {"title": "Promoção de sábado", "icon": "<svg onload=alert(1)>"})
    idea.refresh_from_db()
    assert idea.icon == ""
    # A save that does not send the field leaves the icon alone.
    Idea.objects.filter(pk=idea.pk).update(icon="flame")
    client.post(edit, {"title": "Promoção de domingo"})
    idea.refresh_from_db()
    assert (idea.title, idea.icon) == ("Promoção de domingo", "flame")


def test_every_icon_of_the_library_is_plain_svg_shapes():
    """The markup is printed unescaped, so it may hold shapes and nothing else."""
    import re

    from apps.composer import idea_icons

    assert idea_icons.DEFAULT_ICON in idea_icons.ICONS
    for slug, (_label, markup) in idea_icons.ICONS.items():
        assert re.fullmatch(r"[a-z0-9-]{1,32}", slug)
        tags = set(re.findall(r"<([a-z]+)", markup))
        assert tags <= {"path", "circle", "rect"}, slug
        assert "script" not in markup and "on" not in re.findall(r"\s([a-z]+)=", markup), slug
    assert idea_icons.icon_markup("does-not-exist") == idea_icons.ICONS[idea_icons.DEFAULT_ICON][1]


@pytest.mark.django_db
def test_a_template_becomes_an_idea_drawn_like_it(client):
    from apps.composer import idea_icons
    from apps.composer.models import Idea, IdeaGroup

    user, workspace = _brand()
    client.force_login(user)
    template = IdeaTemplate.objects.create(
        title="Mostre o antes e depois",
        description="Uma transformação real.",
        body="Antes: {{antes}}",
        category="story",
    )
    hidden = IdeaTemplate.objects.create(title="Fora de linha", is_active=False)
    url = reverse("composer:idea_from_template", kwargs={"workspace_id": workspace.id, "template_id": template.id})

    response = client.post(url)

    assert response.headers["Location"] == reverse("composer:create_landing", kwargs={"workspace_id": workspace.id})
    idea = Idea.objects.get(workspace=workspace, title="Mostre o antes e depois")
    assert (idea.icon, idea.icon_tone) == idea_icons.category_style("story")
    assert idea.description == "Antes: {{antes}}"
    assert idea.tags == ["História"]
    assert idea.group == IdeaGroup.objects.filter(workspace=workspace).order_by("position").first()
    # A template taken off the shelf cannot be used, nor one through a brand that is not the user's.
    assert (
        client.post(
            reverse("composer:idea_from_template", kwargs={"workspace_id": workspace.id, "template_id": hidden.id})
        ).status_code
        == 404
    )
    _other, foreign = _brand("outra@example.com")
    assert (
        client.post(
            reverse("composer:idea_from_template", kwargs={"workspace_id": foreign.id, "template_id": template.id})
        ).status_code
        == 403
    )
    assert not Idea.objects.filter(workspace=foreign, title="Mostre o antes e depois").exists()


@pytest.mark.django_db
def test_an_idea_keeps_the_colour_picked_for_its_icon(client):
    from apps.composer.models import Idea

    user, workspace = _brand()
    client.force_login(user)
    kwargs = {"workspace_id": workspace.id}
    client.get(reverse("composer:create_landing", kwargs=kwargs))

    client.post(
        reverse("composer:idea_create", kwargs=kwargs), {"title": "Oferta", "icon": "tag", "icon_tone": "success"}
    )
    idea = Idea.objects.get(title="Oferta")
    assert idea.icon_tone == "success"
    assert "icon-tone-success" in client.get(reverse("composer:create_landing", kwargs=kwargs)).content.decode()

    # A colour that is not on the palette is dropped.
    client.post(
        reverse("composer:idea_edit", kwargs={**kwargs, "idea_id": idea.id}),
        {"title": "Oferta", "icon_tone": "red; background:url(x)"},
    )
    idea.refresh_from_db()
    assert idea.icon_tone == ""


def test_every_template_category_has_an_icon_and_a_tone():
    from apps.composer import idea_icons

    for category in [*IdeaTemplate.Category.values, "all"]:
        icon, tone = idea_icons.CATEGORY_STYLE[category]
        assert icon in idea_icons.ICONS and tone in idea_icons.TONES
