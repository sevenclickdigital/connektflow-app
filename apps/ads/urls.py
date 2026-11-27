from django.urls import path

from . import pinterest_views, views

app_name = "ads"

urlpatterns = [
    path("", views.index, name="index"),
    path("overview/", views.overview, name="overview"),
    path("connect/", views.connect, name="connect"),
    path("settings/", views.connection_settings, name="settings"),
    path("disconnect/", views.disconnect, name="disconnect"),
    path("campaigns/new/", views.campaign_new, name="campaign_new"),
    path("campaigns/<str:campaign_id>/", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<str:campaign_id>/body/", views.campaign_body, name="campaign_body"),
    path("campaigns/<str:campaign_id>/ad-sets/", views.campaign_ad_sets, name="campaign_ad_sets"),
    path("campaigns/<str:campaign_id>/status/", views.campaign_status, name="campaign_status"),
    path("campaigns/<str:campaign_id>/rename/", views.campaign_rename, name="campaign_rename"),
    path("campaigns/<str:campaign_id>/ad-sets/<str:ad_set_id>/", views.ad_set_edit, name="ad_set_edit"),
    # Pinterest Ads
    path("pinterest/", pinterest_views.index, name="pinterest_index"),
    path("pinterest/overview/", pinterest_views.overview, name="pinterest_overview"),
    path("pinterest/connect/", pinterest_views.connect, name="pinterest_connect"),
    path("pinterest/settings/", pinterest_views.connection_settings, name="pinterest_settings"),
    path("pinterest/ad-accounts/new/", pinterest_views.account_create, name="pinterest_account_create"),
    path("pinterest/disconnect/", pinterest_views.disconnect, name="pinterest_disconnect"),
    path("pinterest/campaigns/new/", pinterest_views.campaign_new, name="pinterest_campaign_new"),
    path("pinterest/campaigns/<str:campaign_id>/", pinterest_views.campaign_detail, name="pinterest_campaign_detail"),
    path("pinterest/campaigns/<str:campaign_id>/body/", pinterest_views.campaign_body, name="pinterest_campaign_body"),
    path(
        "pinterest/campaigns/<str:campaign_id>/status/",
        pinterest_views.campaign_status,
        name="pinterest_campaign_status",
    ),
    path("pinterest/campaigns/<str:campaign_id>/edit/", pinterest_views.campaign_edit, name="pinterest_campaign_edit"),
]
