"""URLs of the Team screen of the administrative area.

A separate module and namespace from ``urls.py`` like the other
``staff_urls``, mounted under ``/staff/`` by ``config/staff_urls.py``.
Access is enforced in the views.
"""

from django.urls import path

from . import staff_views

app_name = "accounts_staff"

urlpatterns = [
    path("", staff_views.team, name="team"),
    path("add/", staff_views.team_add, name="team_add"),
    path("<uuid:user_id>/role/", staff_views.team_set_role, name="team_set_role"),
    path("<uuid:user_id>/remove/", staff_views.team_remove, name="team_remove"),
]
