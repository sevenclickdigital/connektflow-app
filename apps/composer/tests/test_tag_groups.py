"""Tag groups: named sets of a brand's tags, made by hand or by saving an
idea started from a special date."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import Tag, TagGroup
from apps.composer.services import tag_groups_payload
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


def _save(client, workspace, **data):
    return client.post(reverse("composer:tag_group_save", kwargs={"workspace_id": workspace.id}), data)


@pytest.mark.django_db
def test_a_group_is_made_by_hand_with_tags_of_any_kind(client):
    user, workspace = _brand()
    client.force_login(user)
    Tag.objects.create(workspace=workspace, name="natal2026", kind=Tag.Kind.HASHTAG)

    response = _save(client, workspace, name="Natal", tags="natal2026, presentes")

    assert response.status_code == 200
    assert response.json()["tags"] == ["natal2026", "presentes"]
    assert tag_groups_payload(workspace.id) == [
        {"id": response.json()["id"], "name": "Natal", "tags": ["natal2026", "presentes"]}
    ]
    # A tag the brand already had keeps its kind; a new one starts internal.
    kinds = dict(Tag.objects.for_workspace(workspace.id).values_list("name", "kind"))
    assert kinds == {"natal2026": Tag.Kind.HASHTAG, "presentes": Tag.Kind.INTERNAL}


@pytest.mark.django_db
def test_a_group_is_renamed_and_its_tags_replaced(client):
    user, workspace = _brand()
    client.force_login(user)
    group_id = _save(client, workspace, name="Natal", tags="natal2026, presentes").json()["id"]

    response = _save(client, workspace, group_id=group_id, name="Fim de ano", tags="presentes, ceia")

    assert response.json() == {"id": group_id, "name": "Fim de ano", "tags": ["ceia", "presentes"]}
    # Leaving a group does not delete the tag.
    assert Tag.objects.for_workspace(workspace.id).filter(name="natal2026").exists()


@pytest.mark.django_db
def test_two_groups_of_a_brand_cannot_share_a_name_and_a_name_is_required(client):
    user, workspace = _brand()
    client.force_login(user)
    _save(client, workspace, name="Natal", tags="a")

    assert _save(client, workspace, name="Natal", tags="b").status_code == 400
    assert _save(client, workspace, name="  ", tags="b").status_code == 400
    assert TagGroup.objects.for_workspace(workspace.id).count() == 1


@pytest.mark.django_db
def test_deleting_a_group_keeps_its_tags(client):
    user, workspace = _brand()
    client.force_login(user)
    group_id = _save(client, workspace, name="Natal", tags="natal2026").json()["id"]

    response = client.post(
        reverse("composer:tag_group_delete", kwargs={"workspace_id": workspace.id, "group_id": group_id})
    )

    assert response.status_code == 200
    assert not TagGroup.objects.for_workspace(workspace.id).exists()
    assert Tag.objects.for_workspace(workspace.id).filter(name="natal2026").exists()


@pytest.mark.django_db
def test_a_brand_does_not_touch_the_groups_of_another(client):
    owner, workspace = _brand()
    client.force_login(owner)
    group_id = _save(client, workspace, name="Natal", tags="natal2026").json()["id"]
    other, other_workspace = _brand("outra@example.com")
    client.force_login(other)

    # Through its own brand, with the other's group id.
    assert _save(client, other_workspace, group_id=group_id, name="Meu", tags="x").status_code == 404
    delete = reverse("composer:tag_group_delete", kwargs={"workspace_id": other_workspace.id, "group_id": group_id})
    assert client.post(delete).status_code == 404
    # Through the other brand's address.
    assert _save(client, workspace, name="Invasor", tags="x").status_code == 403
    assert tag_groups_payload(workspace.id)[0]["name"] == "Natal"
    assert tag_groups_payload(other_workspace.id) == []


@pytest.mark.django_db
def test_a_viewer_does_not_change_the_groups(client):
    viewer, workspace = _brand(role=WorkspaceMembership.WorkspaceRole.VIEWER)
    client.force_login(viewer)

    assert _save(client, workspace, name="Natal", tags="natal2026").status_code == 403
    assert not TagGroup.objects.exists()


@pytest.mark.django_db
def test_an_idea_started_from_a_date_files_the_tags_it_kept_under_the_date(client):
    user, workspace = _brand()
    client.force_login(user)
    kwargs = {"workspace_id": workspace.id}
    client.get(reverse("composer:create_landing", kwargs=kwargs))  # creates the board
    create = reverse("composer:idea_create", kwargs=kwargs)

    # "cultura" was removed in the window, "padaria" was added by hand.
    response = client.post(
        create,
        {
            "title": "Dia da Consciência Negra",
            "tags": "consciêncianegra,igualdade,padaria",
            "tag_group": "Dia da Consciência Negra",
            "tag_group_tags": "consciêncianegra,igualdade,cultura",
        },
        HTTP_ACCEPT="application/json",
    )

    groups = tag_groups_payload(workspace.id)
    assert [(g["name"], g["tags"]) for g in groups] == [("Dia da Consciência Negra", ["consciêncianegra", "igualdade"])]
    assert response.json()["tag_groups"] == groups

    # A second idea for the same date adds to the group, and a tag may sit in two groups.
    client.post(
        create, {"title": "Outra", "tags": "cultura", "tag_group": groups[0]["name"], "tag_group_tags": "cultura"}
    )
    client.post(create, {"title": "Carnaval", "tags": "cultura", "tag_group": "Carnaval", "tag_group_tags": "cultura"})
    assert {g["name"]: g["tags"] for g in tag_groups_payload(workspace.id)} == {
        "Carnaval": ["cultura"],
        "Dia da Consciência Negra": ["consciêncianegra", "cultura", "igualdade"],
    }


@pytest.mark.django_db
def test_an_idea_with_no_date_makes_no_group(client):
    user, workspace = _brand()
    client.force_login(user)
    kwargs = {"workspace_id": workspace.id}
    client.get(reverse("composer:create_landing", kwargs=kwargs))

    client.post(reverse("composer:idea_create", kwargs=kwargs), {"title": "Solta", "tags": "a,b"})

    assert not TagGroup.objects.exists()
