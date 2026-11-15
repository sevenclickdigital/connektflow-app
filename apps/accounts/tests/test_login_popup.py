"""The page that ends a sign-in run in a popup.

It hands the window that opened the popup an address to carry on at, so that
address must never leave the site, and a person who still owes the terms has
to reach the page for the popup to close at all.
"""

import pytest
from django.urls import reverse

DONE = reverse("accounts:login_popup_done")


@pytest.mark.django_db
def test_anonymous_visitor_is_sent_to_the_login_form(client):
    response = client.get(DONE)

    assert response.status_code == 302
    assert reverse("account_login") in response.url


@pytest.mark.django_db
def test_signed_in_user_is_told_where_to_carry_on(client, user):
    client.force_login(user)

    response = client.get(DONE, {"next": "/workspaces/"})

    assert response.status_code == 200
    assert response.context["next_url"] == "/workspaces/"


@pytest.mark.django_db
def test_an_address_outside_the_site_is_ignored(client, user):
    client.force_login(user)

    response = client.get(DONE, {"next": "https://evil.example/steal"})

    assert response.context["next_url"] == "/"


@pytest.mark.django_db
def test_someone_who_still_owes_the_terms_reaches_the_page(client, user):
    user.tos_accepted_at = None
    user.save(update_fields=["tos_accepted_at"])
    client.force_login(user)

    assert client.get(DONE).status_code == 200


@pytest.mark.django_db
def test_login_page_offers_the_popup_address_to_the_google_button(client):
    page = client.get(reverse("account_login")).content.decode()

    assert "data-oauth-popup" in page
    assert f"next={DONE}".replace("/", "%2F") in page or f"next={DONE}" in page
