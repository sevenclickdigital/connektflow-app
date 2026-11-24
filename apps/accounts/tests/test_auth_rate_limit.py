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


@pytest.mark.django_db
class TestClientAddress:
    def test_rotating_forwarded_for_does_not_open_a_new_bucket(self, staff):
        client = Client()
        for i in range(AUTH_RATE_LIMIT):
            client.post(
                "/admin/login/?next=/admin/",
                {"username": "ratelimit-staff@example.com", "password": f"wrong-{i}"},
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i}",
            )

        response = client.post(
            "/admin/login/?next=/admin/",
            {"username": "ratelimit-staff@example.com", "password": PASSWORD},
            HTTP_X_FORWARDED_FOR="10.0.0.250",
        )

        assert response.status_code == 429

    def test_behind_a_proxy_the_hop_it_appended_is_the_client(self, staff, settings):
        settings.ALLAUTH_TRUSTED_PROXY_COUNT = 1
        attacker = Client()
        for i in range(AUTH_RATE_LIMIT):
            attacker.post(
                "/admin/login/?next=/admin/",
                {"username": "ratelimit-staff@example.com", "password": f"wrong-{i}"},
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i}, 203.0.113.7",
            )

        spoofed = attacker.post(
            "/admin/login/?next=/admin/",
            {"username": "ratelimit-staff@example.com", "password": PASSWORD},
            HTTP_X_FORWARDED_FOR="10.0.0.250, 203.0.113.7",
        )
        someone_else = Client().post(
            "/admin/login/?next=/admin/",
            {"username": "ratelimit-staff@example.com", "password": PASSWORD},
            HTTP_X_FORWARDED_FOR="198.51.100.20",
        )

        assert spoofed.status_code == 429
        assert someone_else.status_code == 302


@pytest.mark.django_db
def test_a_wrong_password_is_logged_without_the_address(staff, caplog):
    with caplog.at_level("WARNING", logger="apps.accounts.signals"):
        _admin_login(Client(), "wrong")

    assert "Failed login for ra***@example.com" in caplog.text
    assert "ratelimit-staff@example.com" not in caplog.text


CLOUDFLARE_EDGE = "104.16.0.10"


def _login_via(client, *, peer, cf_ip=None, password="wrong"):
    headers = {"HTTP_X_FORWARDED_FOR": peer}
    if cf_ip:
        headers["HTTP_CF_CONNECTING_IP"] = cf_ip
    return client.post(
        "/admin/login/?next=/admin/",
        {"username": "ratelimit-staff@example.com", "password": password},
        **headers,
    )


@pytest.mark.django_db
class TestBehindCloudflare:
    @pytest.fixture(autouse=True)
    def _traefik_in_front(self, settings):
        settings.ALLAUTH_TRUSTED_PROXY_COUNT = 1

    def test_visitors_sharing_an_edge_have_their_own_limits(self, staff):
        for i in range(AUTH_RATE_LIMIT):
            _login_via(Client(), peer=CLOUDFLARE_EDGE, cf_ip="198.51.100.1", password=f"wrong-{i}")

        blocked = _login_via(Client(), peer=CLOUDFLARE_EDGE, cf_ip="198.51.100.1", password=PASSWORD)
        neighbour = _login_via(Client(), peer=CLOUDFLARE_EDGE, cf_ip="198.51.100.2", password=PASSWORD)

        assert blocked.status_code == 429
        assert neighbour.status_code == 302

    def test_the_header_is_ignored_when_cloudflare_was_bypassed(self, staff):
        """Hitting the server's own address, the visitor writes CF-Connecting-IP itself."""
        for i in range(AUTH_RATE_LIMIT):
            _login_via(Client(), peer="203.0.113.9", cf_ip=f"10.1.0.{i}", password=f"wrong-{i}")

        assert _login_via(Client(), peer="203.0.113.9", cf_ip="10.1.0.99", password=PASSWORD).status_code == 429

    def test_payment_antifraud_gets_the_visitor_not_the_edge(self, rf, settings):
        from apps.accounts.impersonation import client_ip

        request = rf.get("/", HTTP_X_FORWARDED_FOR=CLOUDFLARE_EDGE, HTTP_CF_CONNECTING_IP="198.51.100.7")

        assert client_ip(request) == "198.51.100.7"
