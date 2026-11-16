"""The tag manager: the brand's saved tags with their use, and deleting one
takes it off the ideas and posts that carry it."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import Idea, Post, Tag, TagGroup
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


def _brand(email="dona@example.com", role=WorkspaceMembership.WorkspaceRole.OWNER):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    org = Organization.objects.create(name="Org")
    workspace = Workspace.objects.create(organization=org, name="Brand")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user, workspace


def _url(name, workspace):
    return reverse(f"composer:{name}", kwargs={"workspace_id": workspace.id})


@pytest.mark.django_db
def test_the_manager_lists_each_tag_with_its_use(client):
    user, workspace = _brand()
    client.force_login(user)
    Tag.objects.create(workspace=workspace, name="natal")
    Tag.objects.create(workspace=workspace, name="sobra")
    Idea.objects.create(workspace=workspace, title="Ceia", tags=["natal"])
    Post.objects.create(workspace=workspace, author=user, caption="x", tags=["natal", "outra"])

    tags = client.get(_url("tag_manage_list", workspace)).json()["tags"]

    assert tags == [
        {"name": "natal", "kind": Tag.Kind.INTERNAL, "ideas": 1, "posts": 1},
        {"name": "sobra", "kind": Tag.Kind.INTERNAL, "ideas": 0, "posts": 0},
    ]


@pytest.mark.django_db
def test_deleting_a_tag_takes_it_off_ideas_posts_and_groups(client):
    user, workspace = _brand()
    client.force_login(user)
    tag = Tag.objects.create(workspace=workspace, name="natal")
    group = TagGroup.objects.create(workspace=workspace, name="Fim de ano")
    group.tags.add(tag)
    idea = Idea.objects.create(workspace=workspace, title="Ceia", tags=["natal", "ceia"])
    post = Post.objects.create(workspace=workspace, author=user, caption="x", tags=["natal"])

    response = client.post(_url("tag_delete", workspace), {"name": "natal"})

    assert response.status_code == 200
    assert not Tag.objects.filter(workspace=workspace, name="natal").exists()
    idea.refresh_from_db()
    post.refresh_from_db()
    assert idea.tags == ["ceia"]
    assert post.tags == []
    assert list(group.tags.all()) == []


@pytest.mark.django_db
def test_deleting_leaves_the_same_tag_of_another_brand_alone(client):
    user, workspace = _brand()
    other_user, other = _brand(email="outra@example.com")
    client.force_login(user)
    Tag.objects.create(workspace=workspace, name="natal")
    Tag.objects.create(workspace=other, name="natal")
    other_idea = Idea.objects.create(workspace=other, title="Deles", tags=["natal"])

    client.post(_url("tag_delete", workspace), {"name": "natal"})

    assert Tag.objects.filter(workspace=other, name="natal").exists()
    other_idea.refresh_from_db()
    assert other_idea.tags == ["natal"]


@pytest.mark.django_db
def test_a_missing_tag_answers_not_found(client):
    user, workspace = _brand()
    client.force_login(user)

    response = client.post(_url("tag_delete", workspace), {"name": "nada"})

    assert response.status_code == 404


@pytest.mark.django_db
def test_a_viewer_cannot_delete_tags(client):
    user, workspace = _brand(role=WorkspaceMembership.WorkspaceRole.VIEWER)
    client.force_login(user)
    Tag.objects.create(workspace=workspace, name="natal")

    response = client.post(_url("tag_delete", workspace), {"name": "natal"})

    assert response.status_code == 403
    assert Tag.objects.filter(workspace=workspace, name="natal").exists()
