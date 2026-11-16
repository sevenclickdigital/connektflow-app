"""Staff-only URLs for the shelf of special dates.

A separate module and namespace from ``urls.py`` for the same reason
``apps.billing.staff_urls`` is: these paths carry no ``workspace_id``.
Access is enforced in the views.
"""

from django.urls import path

from . import staff_views

app_name = "calendar_staff"

urlpatterns = [
    path("", staff_views.dates, name="dates"),
    path("segments/new/", staff_views.segment_create, name="segment_create"),
    path("segments/<uuid:segment_id>/", staff_views.segment_update, name="segment_update"),
    path("segments/<uuid:segment_id>/delete/", staff_views.segment_delete, name="segment_delete"),
    path("dates/new/", staff_views.date_create, name="date_create"),
    path("dates/<uuid:date_id>/delete/", staff_views.date_delete, name="date_delete"),
]
