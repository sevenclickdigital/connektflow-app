"""Every route of the administrative area answers only to the team.

The administrative area is everything ``config/staff_urls.py`` mounts under
``/staff/``. Its paths carry no organization or workspace, so no membership
check stands in front of them: each view must carry ``staff_required`` or
``superadmin_required`` itself. This walks every route mounted there — so a
new screen is covered without touching this file — and asks it, with GET and
POST, as a visitor and as an organization owner who is not staff. It also
checks that no ``staff_urls`` module is mounted anywhere else.
"""

from __future__ import annotations

import uuid

import pytest
from django.urls import URLResolver, get_resolver, reverse

from apps.accounts.operators import STAFF_AREA_PREFIX

STAFF_URLCONF_SUFFIX = ".staff_urls"


def _area():
    """The resolver of the administrative area in the root URLconf."""
    return next(
        entry
        for entry in get_resolver().url_patterns
        if isinstance(entry, URLResolver) and str(entry.pattern) == STAFF_AREA_PREFIX
    )


def _staff_routes():
    routes = []
    for entry in _area().url_patterns:
        for pattern in entry.url_patterns:
            kwargs = {}
            for name, converter in pattern.pattern.converters.items():
                kind = type(converter).__name__
                kwargs[name] = uuid.uuid4() if kind == "UUIDConverter" else 1 if kind == "IntConverter" else "x"
            url = reverse(f"{entry.namespace}:{pattern.name}", kwargs=kwargs)
            routes.append(pytest.param(url, id=f"{entry.namespace}:{pattern.name}"))
    return routes


ROUTES = _staff_routes()


def test_the_administrative_area_has_routes_to_check():
    assert len(ROUTES) > 20


def test_every_route_of_the_area_is_under_its_prefix():
    assert all(route.values[0].startswith(f"/{STAFF_AREA_PREFIX}") for route in ROUTES)


def test_no_staff_module_is_mounted_outside_the_area():
    """A ``staff_urls`` module mounted at the root would be a team screen the
    prefix rule does not cover."""
    outside = [
        entry.urlconf_module.__name__
        for entry in get_resolver().url_patterns
        if isinstance(entry, URLResolver)
        and entry is not _area()
        and getattr(entry.urlconf_module, "__name__", "").endswith(STAFF_URLCONF_SUFFIX)
    ]

    assert outside == []


@pytest.mark.django_db
@pytest.mark.parametrize("url", ROUTES)
def test_a_visitor_is_sent_to_log_in(client, url):
    for method in (client.get, client.post):
        response = method(url)
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
@pytest.mark.parametrize("url", ROUTES)
def test_a_customer_who_is_not_staff_is_refused(client, org_owner, url):
    assert not org_owner.is_staff
    client.force_login(org_owner)
    for method in (client.get, client.post):
        assert method(url).status_code == 403
