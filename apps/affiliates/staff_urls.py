"""Staff-only affiliate URLs.

A separate module and namespace from ``urls.py`` for the same reason
``apps.billing.staff_urls`` is. Access is enforced in the views.
"""

from django.urls import path

from . import staff_views

app_name = "affiliates_staff"

urlpatterns = [
    path("", staff_views.affiliates, name="affiliates"),
    path("new/", staff_views.save_affiliate, name="create"),
    path("<uuid:affiliate_id>/edit/", staff_views.save_affiliate, name="edit"),
    path("<uuid:affiliate_id>/toggle/", staff_views.toggle_affiliate, name="toggle"),
    path("payouts/<uuid:payout_id>/resolve/", staff_views.resolve_payout, name="resolve_payout"),
]
