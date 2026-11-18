"""The Meta Login callback, outside any workspace: its URL is registered in the Meta app."""

from django.urls import path

from . import views

app_name = "ads_callback"

urlpatterns = [
    path("meta/callback/", views.meta_callback, name="meta_callback"),
]
