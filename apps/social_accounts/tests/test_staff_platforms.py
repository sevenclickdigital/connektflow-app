"""The operator's Platforms screen: who may reach it and what a switch hides."""

import json

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.social_accounts.models import PlatformVisibility

pytestmark = pytest.mark.django_db


def _user(email, *, staff=False):
    return User.objects.create_user(
        email=email, password="x", tos_accepted_at=timezone.now(), is_staff=staff, is_superuser=staff
    )


def test_only_staff_reach_the_screen_and_the_switch(client):
    client.force_login(_user("member@example.com"))

    assert client.get(reverse("social_accounts_staff:platforms")).status_code == 403
    response = client.post(reverse("social_accounts_staff:platform_toggle"), {"platform": "tiktok"})
    assert response.status_code == 403
    assert "tiktok" in dict(PlatformVisibility.visible_choices())


def test_the_screen_lists_every_platform_with_its_switch(client):
    client.force_login(_user("op@example.com", staff=True))
    PlatformVisibility.set_visible("x", False)

    response = client.get(reverse("social_accounts_staff:platforms"))

    shown = {p["value"]: p["visible"] for p in response.context["platforms"]}
    assert shown["x"] is False and shown["tiktok"] is True
    assert reverse("social_accounts_staff:platform_toggle") in response.content.decode()


def test_a_switch_hides_and_shows_a_platform_on_the_connect_surfaces(client):
    client.force_login(_user("op@example.com", staff=True))
    toggle = reverse("social_accounts_staff:platform_toggle")

    response = client.post(toggle, {"platform": "tiktok"})

    assert response.status_code == 204
    assert json.loads(response["HX-Trigger"])["showToast"]["tone"] == "info"
    assert "tiktok" not in dict(PlatformVisibility.visible_choices())

    response = client.post(toggle, {"platform": "tiktok", "visible": "on"})

    assert json.loads(response["HX-Trigger"])["showToast"]["tone"] == "success"
    assert "tiktok" in dict(PlatformVisibility.visible_choices())


def test_an_unknown_platform_is_refused(client):
    client.force_login(_user("op@example.com", staff=True))

    response = client.post(reverse("social_accounts_staff:platform_toggle"), {"platform": "myspace"})

    assert response.status_code == 400
    assert not PlatformVisibility.objects.filter(platform="myspace").exists()


def test_a_hidden_opportunity_source_leaves_only_the_brands_that_have_not_linked_it(client, organization):
    from apps.radar.models import OpportunityConnection
    from apps.radar.sources import source_cards
    from apps.workspaces.models import Workspace

    linked = Workspace.objects.create(organization=organization, name="Linked")
    fresh = Workspace.objects.create(organization=organization, name="Fresh")
    OpportunityConnection.objects.create(
        workspace=linked,
        source=OpportunityConnection.Source.SHOPEE_AFFILIATE,
        account_label="18340070022",
        credentials={"app_id": "18340070022", "secret": "s3cret"},
    )
    client.force_login(_user("op@example.com", staff=True))
    toggle = reverse("social_accounts_staff:platform_toggle")

    for key in ("shopee_affiliate", "shein"):
        assert client.post(toggle, {"platform": key}).status_code == 204

    def keys(workspace):
        return [card.source.key for card in source_cards(workspace.id)]

    assert "shopee_affiliate" not in keys(fresh) and "shein" not in keys(fresh)
    assert "mercado_livre" in keys(fresh)
    assert "shopee_affiliate" in keys(linked)
    shown = {
        s["value"]: s["visible"] for s in client.get(reverse("social_accounts_staff:platforms")).context["sources"]
    }
    assert shown["shein"] is False and shown["tiktok_shop"] is True
