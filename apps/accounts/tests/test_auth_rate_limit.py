"""The per-address limit on password-checking endpoints (AuthRateLimitMiddleware)."""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.accounts.middleware import AUTH_RATE_LIMIT
from apps.accounts.models import User

PASSWORD = "a-long-test-password"


@pytest.fixture(autouse=True)
def _empty_rate_limit_buckets():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def staff(db):
    return User.objects.create_user(
        email="ratelimit-staff@example.com", password=PASSWORD, is_staff=True, tos_accepted_at=timezone.now()
    )


def _admin_login(client, password):
    return client.post("/admin/login/?next=/admin/", {"username": "ratelimit-staff@example.com", "password": password})


@pytest.mark.django_db
class TestAdminLoginIsThrottled:
    def test_guessing_is_cut_off_at_the_limit(self, staff):
        client = Client()

        statuses = [_admin_login(client, f"wrong-{i}").status_code for i in range(AUTH_RATE_LIMIT + 2)]

        assert statuses[:AUTH_RATE_LIMIT] == [200] * AUTH_RATE_LIMIT
        assert statuses[AUTH_RATE_LIMIT:] == [429, 429]
        # Even the right password is refused once the address is over the limit.
        assert _admin_login(client, PASSWORD).status_code == 429
        assert "_auth_user_id" not in client.session

    def test_staff_signs_in_under_the_limit(self, staff):
        client = Client()

        response = _admin_login(client, PASSWORD)

        assert response.status_code == 302
        assert client.session["_auth_user_id"] == str(staff.pk)

    def test_public_login_shares_the_same_bucket(self, staff):
        client = Client()
        for i in range(AUTH_RATE_LIMIT):
            client.post("/accounts/login/", {"login": "nobody@example.com", "password": f"wrong-{i}"})

        assert _admin_login(client, PASSWORD).status_code == 429
