"""Every route of the administrative area answers only to staff.

The administrative area (layouts/admin_shell.html) is made of the
``apps.<app>.staff_urls`` modules mounted in config/urls.py. Their paths
carry no organization or workspace, so no membership check stands in front
of them: each view must check ``is_staff`` itself. This walks every route of
every such module — found from the root URLconf, so a new module is covered
without touching this file — and asks it, with GET and POST, as a visitor
and as an organization owner who is not staff.
"""

from __future__ import annotations

import uuid

import pytest
from django.urls import URLResolver, get_resolver, reverse

STAFF_URLCONF_SUFFIX = ".staff_urls"


def _staff_routes():
    routes = []
    for entry in get_resolver().url_patterns:
        if not isinstance(entry, URLResolver):
            continue
        module = getattr(entry.urlconf_module, "__name__", "")
        if not module.endswith(STAFF_URLCONF_SUFFIX):
            continue
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
