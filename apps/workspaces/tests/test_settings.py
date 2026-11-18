"""Brand settings → General: who may change the name and the logo, and what a logo may be."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from apps.members.models import WorkspaceMembership
from apps.workspaces.models import Workspace
from apps.workspaces.tests.test_brand_team import _person

ROLE = WorkspaceMembership.WorkspaceRole

pytestmark = pytest.mark.django_db


@pytest.fixture
def brand(organization, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return Workspace.objects.create(organization=organization, name="Brand Original")


def _url(brand):
    return reverse("workspaces:settings", kwargs={"workspace_id": brand.id})


def _png():
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _logo(name="logo.png", body=None):
    return SimpleUploadedFile(name, body or _png(), content_type="image/png")


@pytest.mark.parametrize("role", [ROLE.VIEWER, ROLE.CLIENT, ROLE.EDITOR])
def test_a_member_without_settings_authority_cannot_change_name_or_logo(client, organization, brand, role):
    brand.icon.save("kept.png", SimpleUploadedFile("kept.png", _png()), save=True)
    stored = brand.icon.name
    client.force_login(_person("low@a.com", organization, brand=brand, role=role))

    responses = [
        client.post(_url(brand), {"name": "Renamed"}),
        client.post(_url(brand), {"icon": _logo()}),
        client.post(_url(brand), {"delete_icon": "1"}),
    ]

    assert [r.status_code for r in responses] == [404, 404, 404]
    brand.refresh_from_db()
    assert brand.name == "Brand Original"
    assert brand.icon.name == stored
    assert brand.icon.storage.exists(stored)


def test_a_member_without_settings_authority_is_not_shown_the_forms(client, organization, brand):
    client.force_login(_person("viewer@a.com", organization, brand=brand, role=ROLE.VIEWER))

    page = client.get(_url(brand)).content.decode()

    assert 'id="name"' not in page
    assert 'name="icon"' not in page


@pytest.mark.parametrize("role", [ROLE.OWNER, ROLE.MANAGER])
def test_owner_and_manager_change_name_and_logo(client, organization, brand, role):
    client.force_login(_person("boss@a.com", organization, brand=brand, role=role))

    assert 'id="name"' in client.get(_url(brand)).content.decode()
    response = client.post(_url(brand), {"name": "Renamed", "icon": _logo()})

    assert response.status_code == 302
    brand.refresh_from_db()
    assert brand.name == "Renamed"
    assert brand.icon.name.endswith(".png")


def test_a_logo_that_is_not_an_image_is_refused(client, organization, brand):
    client.force_login(_person("owner@a.com", organization, brand=brand, role=ROLE.OWNER))

    client.post(_url(brand), {"icon": _logo("logo.html", b"<!doctype html><p>not an image</p>")})

    brand.refresh_from_db()
    assert not brand.icon


def test_a_logo_is_stored_under_the_extension_of_its_bytes(client, organization, brand):
    client.force_login(_person("owner@a.com", organization, brand=brand, role=ROLE.OWNER))

    client.post(_url(brand), {"icon": _logo("logo.html")})

    brand.refresh_from_db()
    assert brand.icon.name.startswith("workspaces/icons/")
    assert brand.icon.name.endswith(".png")
