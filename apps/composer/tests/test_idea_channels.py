"""An idea's channels: the brand's connected accounts it is meant for, each on
its formats (Feed / Story / Reels), filled in from a template's networks and
used when the idea becomes a post."""

import json

import pytest
from django.urls import reverse

from apps.composer.models import Idea, IdeaGroup, IdeaTemplate
from apps.composer.tests.test_idea_templates import _brand
from apps.social_accounts.models import SocialAccount


def _account(workspace, platform, status=SocialAccount.ConnectionStatus.CONNECTED):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-{workspace.id}",
        account_name=platform,
        connection_status=status,
    )


def _url(name, workspace, **kwargs):
    return reverse(f"composer:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


@pytest.mark.django_db
def test_an_idea_keeps_only_the_brands_connected_channels(client):
    user, workspace = _brand()
    _other_user, other = _brand("outra@example.com")
    client.force_login(user)
    instagram = _account(workspace, "instagram")
    _account(workspace, "facebook")
    foreign = _account(other, "instagram")
    group = IdeaGroup.objects.create(workspace=workspace, name="Ideias")

    picked = {str(instagram.id): ["reel", "default", "nope"], str(foreign.id): ["default"], "nao-e-id": ["default"]}
    client.post(
        _url("idea_create", workspace),
        {"title": "Com canais", "group": str(group.id), "channel_formats": json.dumps(picked)},
    )
    idea = Idea.objects.get(workspace=workspace, title="Com canais")
    assert list(idea.channels.all()) == [instagram]
    # In the account's own order, without what it does not offer.
    assert idea.channel_formats == {str(instagram.id): ["default", "reel"]}

    client.post(_url("idea_edit", workspace, idea_id=idea.id), {"title": "Com canais", "channel_formats": "{}"})
    idea.refresh_from_db()
    assert not idea.channels.exists() and idea.channel_formats == {}


@pytest.mark.django_db
def test_an_idea_from_a_template_comes_with_its_networks_channels(client):
    user, workspace = _brand()
    client.force_login(user)
    instagram, _facebook = _account(workspace, "instagram"), _account(workspace, "facebook")
    template = IdeaTemplate.objects.create(
        title="Só Instagram",
        body="Texto",
        category="tip",
        networks=["instagram"],
        network_formats={"instagram": ["story"]},
    )

    client.post(_url("idea_from_template", workspace, template_id=template.id))

    idea = Idea.objects.get(workspace=workspace, title="Só Instagram")
    assert list(idea.channels.all()) == [instagram]
    assert idea.channel_formats == {str(instagram.id): ["story"]}


@pytest.mark.django_db
def test_the_post_starts_on_the_ideas_channels_or_on_every_connected_one(client):
    user, workspace = _brand()
    client.force_login(user)
    instagram, facebook = _account(workspace, "instagram"), _account(workspace, "facebook")
    picked = Idea.objects.create(
        workspace=workspace, title="Escolhida", channel_formats={str(instagram.id): ["default", "reel"]}
    )
    picked.channels.set([instagram])
    open_one = Idea.objects.create(workspace=workspace, title="Livre")

    client.post(_url("idea_create_post", workspace, idea_id=picked.id))
    client.post(_url("idea_create_post", workspace, idea_id=open_one.id))

    picked.refresh_from_db()
    open_one.refresh_from_db()
    assert {(pp.social_account, pp.format) for pp in picked.post.platform_posts.all()} == {
        (instagram, "default"),
        (instagram, "reel"),
    }
    assert {pp.social_account for pp in open_one.post.platform_posts.all()} == {instagram, facebook}
