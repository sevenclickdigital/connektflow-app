"""The site root, opened with no session.

Google's OAuth brand verification and Meta's App Review reject an app whose
home page asks for a login, so the root has to present the product and link to
the privacy policy to an anonymous visitor.
"""

import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_anonymous_visitor_reads_the_presentation_page(client):
    response = client.get("/")

    assert response.status_code == 200
    page = response.content.decode()
    assert reverse("privacy_policy") in page
    assert reverse("account_login") in page


@pytest.mark.django_db
def test_signed_in_user_still_lands_in_the_app(client, user):
    client.force_login(user)

    response = client.get("/")

    assert reverse("privacy_policy") not in response.content.decode()
