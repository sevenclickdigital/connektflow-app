"""The ads sign-in callbacks, outside any workspace: each URL is registered in its platform's app."""

from django.urls import path

from . import pinterest_views, views

app_name = "ads_callback"

urlpatterns = [
    path("meta/callback/", views.meta_callback, name="meta_callback"),
    path("pinterest/callback/", pinterest_views.callback, name="pinterest_callback"),
]
