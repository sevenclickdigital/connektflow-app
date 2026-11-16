"""Special dates by segment: global dates reach every brand, a segment's
dates reach only the brands that chose it, and only the operator edits them."""

from datetime import date

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.calendar.holidays import special_dates_between
from apps.calendar.models import DateSegment, SpecialDate, WorkspaceDateSegment
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

YEAR = (date(2027, 1, 1), date(2027, 12, 31))


def _brand(email, role=WorkspaceMembership.WorkspaceRole.OWNER):
    user = User.objects.create_user(email=email, password="x", tos_accepted_at=timezone.now())
    org = Organization.objects.create(name=f"Org of {email}")
    workspace = Workspace.objects.create(organization=org, name="Brand")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user, workspace


def _names(workspace=None):
    return [d["name"] for d in special_dates_between(*YEAR, workspace)]


@pytest.fixture
def pizza(db):
    segment = DateSegment.objects.create(name="Pizzarias")
    SpecialDate.objects.create(segment=segment, name="Dia do Forno a Lenha", month=3, day=3)
    return segment


@pytest.mark.django_db
def test_a_segment_date_reaches_only_the_brands_that_chose_it(pizza):
    _user, follower = _brand("a@example.com")
    _other, bystander = _brand("b@example.com")
    WorkspaceDateSegment.objects.create(workspace=follower, segment=pizza)

    assert "Dia do Forno a Lenha" in _names(follower)
    assert "Dia do Forno a Lenha" not in _names(bystander)
    assert "Dia do Forno a Lenha" not in _names()


@pytest.mark.django_db
def test_a_global_date_reaches_every_brand_beside_the_built_in_ones():
    SpecialDate.objects.create(name="Aniversário do Connekt Flow", month=6, day=2)
    _user, workspace = _brand("a@example.com")

    names = _names(workspace)

    assert "Aniversário do Connekt Flow" in names
    assert len(names) > 1  # the holidays that ship with the product are still there


@pytest.mark.django_db
def test_hidden_segments_and_dates_are_not_shown(pizza):
    _user, workspace = _brand("a@example.com")
    WorkspaceDateSegment.objects.create(workspace=workspace, segment=pizza)
    SpecialDate.objects.create(segment=pizza, name="Data desligada", month=4, day=4, is_active=False)
    assert "Data desligada" not in _names(workspace)

    pizza.is_active = False
    pizza.save()
    assert "Dia do Forno a Lenha" not in _names(workspace)


@pytest.mark.django_db
def test_29_february_is_skipped_in_a_common_year():
    SpecialDate.objects.create(name="Dia Raro", month=2, day=29)
    assert "Dia Raro" not in _names()
    assert "Dia Raro" in [d["name"] for d in special_dates_between(date(2028, 1, 1), date(2028, 12, 31))]


@pytest.mark.django_db
def test_a_brand_follows_and_unfollows_a_segment(client, pizza):
    user, workspace = _brand("a@example.com")
    client.force_login(user)
    url = reverse("calendar:date_segment_toggle", kwargs={"workspace_id": workspace.id, "segment_id": pizza.id})

    response = client.post(url)
    assert response.status_code == 302
    assert response.headers["Location"] == reverse("composer:create_dates", kwargs={"workspace_id": workspace.id})
    assert WorkspaceDateSegment.objects.filter(workspace=workspace, segment=pizza).exists()

    client.post(url)
    assert not WorkspaceDateSegment.objects.filter(workspace=workspace, segment=pizza).exists()


@pytest.mark.django_db
def test_nobody_chooses_segments_for_a_brand_they_do_not_belong_to(client, pizza):
    _owner, workspace = _brand("a@example.com")
    stranger, _own = _brand("b@example.com")
    client.force_login(stranger)

    response = client.post(
        reverse("calendar:date_segment_toggle", kwargs={"workspace_id": workspace.id, "segment_id": pizza.id})
    )

    assert response.status_code == 403
    assert not WorkspaceDateSegment.objects.filter(workspace=workspace).exists()


@pytest.mark.django_db
def test_the_dates_tab_lists_the_segments_and_the_year_ahead(client, pizza):
    user, workspace = _brand("a@example.com")
    WorkspaceDateSegment.objects.create(workspace=workspace, segment=pizza)
    client.force_login(user)

    context = client.get(reverse("composer:create_dates", kwargs={"workspace_id": workspace.id})).context

    assert context["tab"] == "dates"
    assert {"Pizzarias": True}.items() <= {i["segment"].name: i["chosen"] for i in context["date_segments"]}.items()
    assert "Dia do Forno a Lenha" in [d["name"] for d in context["special_dates_ahead"]]


@pytest.mark.django_db
def test_only_the_operator_edits_the_shelf(client, pizza):
    customer, _workspace = _brand("a@example.com")
    client.force_login(customer)
    assert client.get(reverse("calendar_staff:dates")).status_code in (302, 403, 404)
    client.post(reverse("calendar_staff:date_create"), {"name": "Invasão", "day": 1, "month": 1})
    client.post(reverse("calendar_staff:segment_delete", kwargs={"segment_id": pizza.id}))
    assert not SpecialDate.objects.filter(name="Invasão").exists()
    assert DateSegment.objects.filter(id=pizza.id).exists()


@pytest.mark.django_db
def test_the_operator_adds_and_removes_segments_and_dates(client):
    operator = User.objects.create_user(
        email="op@example.com", password="x", tos_accepted_at=timezone.now(), is_staff=True
    )
    client.force_login(operator)

    assert client.get(reverse("calendar_staff:dates")).status_code == 200
    client.post(reverse("calendar_staff:segment_create"), {"name": "Academias", "emoji": "🏋️"})
    segment = DateSegment.objects.get(name="Academias")
    client.post(
        reverse("calendar_staff:date_create"), {"segment": segment.id, "name": "Dia do Treino", "day": 5, "month": 5}
    )
    client.post(
        reverse("calendar_staff:date_create"),
        {
            "name": "Data Geral",
            "day": 6,
            "month": 6,
            "kind": "seasonal",
            "description": "Uma data de todos.",
            "suggestions": "Primeira ideia\n\nSegunda ideia",
            "tags": "#geral, festa",
        },
    )
    # 31 February does not exist; nothing is saved.
    client.post(reverse("calendar_staff:date_create"), {"name": "Data Impossível", "day": 31, "month": 2})

    assert SpecialDate.objects.get(name="Dia do Treino").segment == segment
    general = SpecialDate.objects.get(name="Data Geral")
    assert general.segment is None
    assert (general.kind, general.suggestion_list, general.tag_list) == (
        "seasonal",
        ["Primeira ideia", "Segunda ideia"],
        ["geral", "festa"],
    )
    # A category that does not exist falls back to the default one.
    assert SpecialDate.objects.get(name="Dia do Treino").kind == "commercial"
    assert not SpecialDate.objects.filter(name="Data Impossível").exists()

    client.post(reverse("calendar_staff:segment_update", kwargs={"segment_id": segment.id}), {"is_active": "0"})
    segment.refresh_from_db()
    assert segment.is_active is False

    client.post(reverse("calendar_staff:segment_delete", kwargs={"segment_id": segment.id}))
    assert not SpecialDate.objects.filter(name="Dia do Treino").exists()


@pytest.mark.django_db
def test_the_tab_gets_each_date_with_its_category_and_content_ideas(pizza):
    from apps.calendar.services import special_dates_ahead

    _user, workspace = _brand("a@example.com")
    WorkspaceDateSegment.objects.create(workspace=workspace, segment=pizza)
    SpecialDate.objects.filter(segment=pizza).update(suggestions="Forno aceso ao vivo", tags="pizza")

    entries = {e["name"]: e for e in special_dates_ahead(workspace, date(2027, 1, 1))}

    oven = entries["Dia do Forno a Lenha"]
    assert (oven["iso"], oven["kind"], oven["segment"]) == ("2027-03-03", "commercial", "Pizzarias")
    assert (oven["suggestions"], oven["tags"]) == (["Forno aceso ao vivo"], ["pizza"])
    # A date with no ideas of its own gets the generic ones of its category.
    built_in = next(e for e in entries.values() if not e["segment"])
    assert built_in["suggestions"] and built_in["kind"] in ("national", "commercial")


@pytest.mark.django_db
def test_every_built_in_date_says_what_it_is():
    from apps.calendar.holidays import BUILT_IN_CONTENT

    built_in = [d for d in special_dates_between(*YEAR) if d["built_in"]]

    assert {d["key"] for d in built_in} == {f"b:{slug}" for slug in BUILT_IN_CONTENT}
    assert all(d["description"] and d["tags"] and d["suggestions"] for d in built_in)


@pytest.mark.django_db
def test_a_brand_saves_and_drops_a_favorite_date(client):
    from apps.calendar.models import FavoriteSpecialDate
    from apps.calendar.services import special_dates_ahead

    user, workspace = _brand("a@example.com")
    client.force_login(user)
    url = reverse("calendar:special_date_favorite", kwargs={"workspace_id": workspace.id})

    assert client.post(url, {"key": "b:black-friday"}).json() == {"favorite": True}
    saved = [e["name"] for e in special_dates_ahead(workspace, date(2027, 1, 1)) if e["favorite"]]
    assert saved == ["Black Friday"]

    assert client.post(url, {"key": "b:black-friday"}).json() == {"favorite": False}
    assert not FavoriteSpecialDate.objects.filter(workspace=workspace).exists()
    # Not the shape of a key: refused rather than stored.
    assert client.post(url, {"key": "<script>"}).status_code == 400


@pytest.mark.django_db
def test_favorites_belong_to_the_brand_that_saved_them(client):
    from apps.calendar.models import FavoriteSpecialDate
    from apps.calendar.services import special_dates_ahead

    _owner, workspace = _brand("a@example.com")
    stranger, own = _brand("b@example.com")
    FavoriteSpecialDate.objects.create(workspace=workspace, key="b:christmas")
    client.force_login(stranger)

    response = client.post(
        reverse("calendar:special_date_favorite", kwargs={"workspace_id": workspace.id}), {"key": "b:christmas"}
    )

    assert response.status_code == 403
    assert FavoriteSpecialDate.objects.filter(workspace=workspace, key="b:christmas").exists()
    assert not any(e["favorite"] for e in special_dates_ahead(own, date(2027, 1, 1)))


@pytest.mark.django_db
def test_the_operator_may_send_a_picture_and_only_a_picture(client, settings, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile

    settings.MEDIA_ROOT = tmp_path
    settings.STORAGES = {**settings.STORAGES, "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}}
    operator = User.objects.create_user(
        email="op@example.com", password="x", tos_accepted_at=timezone.now(), is_staff=True
    )
    client.force_login(operator)
    # The smallest valid PNG: one transparent pixel.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
    )
    url = reverse("calendar_staff:date_create")

    client.post(url, {"name": "Com Foto", "day": 2, "month": 2, "image": SimpleUploadedFile("a.png", png, "image/png")})
    client.post(
        url,
        {"name": "Com Script", "day": 3, "month": 3, "image": SimpleUploadedFile("a.svg", b"<svg/>", "image/svg+xml")},
    )

    assert SpecialDate.objects.get(name="Com Foto").image
    assert not SpecialDate.objects.filter(name="Com Script").exists()


@pytest.mark.django_db
def test_the_brand_settings_choose_the_segments(client, pizza):
    other = DateSegment.objects.create(name="Sorveterias")
    hidden = DateSegment.objects.create(name="Fora de linha", is_active=False)
    user, workspace = _brand("a@example.com")
    WorkspaceDateSegment.objects.create(workspace=workspace, segment=other)
    client.force_login(user)
    url = reverse("workspaces:settings", kwargs={"workspace_id": workspace.id})

    assert "Pizzarias" in client.get(url).content.decode()
    client.post(url, {"action": "update_segments", "segments": [str(pizza.id), str(hidden.id)]})

    # Exactly what was ticked, and only among the segments on offer.
    followed = set(WorkspaceDateSegment.objects.filter(workspace=workspace).values_list("segment__name", flat=True))
    assert followed == {"Pizzarias"}


@pytest.mark.django_db
def test_a_viewer_does_not_change_the_segments_of_the_brand(client, pizza):
    _owner, workspace = _brand("a@example.com")
    viewer = User.objects.create_user(email="v@example.com", password="x", tos_accepted_at=timezone.now())
    WorkspaceMembership.objects.create(
        user=viewer, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
    )
    client.force_login(viewer)

    client.post(
        reverse("workspaces:settings", kwargs={"workspace_id": workspace.id}),
        {"action": "update_segments", "segments": [str(pizza.id)]},
    )

    assert not WorkspaceDateSegment.objects.filter(workspace=workspace).exists()
