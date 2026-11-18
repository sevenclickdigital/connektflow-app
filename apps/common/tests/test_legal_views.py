"""Public legal pages: the addresses the Meta and Google app settings point at."""

import pytest

from apps.common.legal_views import PRIVACY_POLICY_URL, TERMS_OF_SERVICE_URL


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ("/privacidade/", PRIVACY_POLICY_URL),
        ("/privacy/", PRIVACY_POLICY_URL),
        ("/termos/", TERMS_OF_SERVICE_URL),
        ("/terms/", TERMS_OF_SERVICE_URL),
    ],
)
def test_privacy_and_terms_answer_anonymously_with_the_public_site(client, path, target):
    response = client.get(path)

    assert response.status_code == 301
    assert response["Location"] == target


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/exclusao-de-dados/", "/data-deletion/"])
def test_the_data_deletion_page_stays_in_the_app_and_links_the_policy_on_the_site(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert PRIVACY_POLICY_URL in response.content.decode()
