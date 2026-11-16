"""Staff-only URLs for the market hashtags.

A separate module and namespace from ``urls.py`` for the same reason
``apps.billing.staff_urls`` is: these paths carry no ``workspace_id``.
Access is enforced in the views.
"""

from django.urls import path

from . import staff_views

app_name = "radar_staff"

urlpatterns = [
    path("", staff_views.hashtags, name="market_hashtags"),
    path("add/", staff_views.hashtag_add, name="market_hashtag_add"),
    path("<uuid:hashtag_id>/delete/", staff_views.hashtag_delete, name="market_hashtag_delete"),
    path("account/", staff_views.collector_account, name="market_hashtag_account"),
    path("collect/", staff_views.collect_now, name="market_hashtag_collect"),
]
