from django.urls import path

from . import views

app_name = "ads"

urlpatterns = [
    path("", views.index, name="index"),
    path("connect/", views.connect, name="connect"),
    path("settings/", views.connection_settings, name="settings"),
    path("disconnect/", views.disconnect, name="disconnect"),
    path("campaigns/new/", views.campaign_new, name="campaign_new"),
    path("campaigns/<str:campaign_id>/", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<str:campaign_id>/status/", views.campaign_status, name="campaign_status"),
    path("campaigns/<str:campaign_id>/rename/", views.campaign_rename, name="campaign_rename"),
    path("campaigns/<str:campaign_id>/ad-sets/<str:ad_set_id>/", views.ad_set_edit, name="ad_set_edit"),
]
