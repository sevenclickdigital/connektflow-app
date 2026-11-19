"""Staff-only URLs for which networks customers may connect.

A separate module and namespace from ``urls.py`` for the same reason
``apps.billing.staff_urls`` is: these paths carry no ``workspace_id``.
Access is enforced in the views.
"""

from django.urls import path

from . import staff_views

app_name = "social_accounts_staff"

urlpatterns = [
    path("", staff_views.platforms_list, name="platforms"),
    path("toggle/", staff_views.platform_toggle, name="platform_toggle"),
]
