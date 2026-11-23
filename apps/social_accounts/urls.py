from django.urls import path

from . import views

app_name = "social_accounts"

urlpatterns = [
    # Workspace-scoped views
    path(
        "<uuid:workspace_id>/",
        views.account_list,
        name="list",
    ),
    path(
        "<uuid:workspace_id>/connect/",
        views.connect_platform,
        name="connect",
    ),
    path(
        "<uuid:workspace_id>/connect/bluesky/",
        views.connect_bluesky,
        name="connect_bluesky",
    ),
    path(
        "<uuid:workspace_id>/connect/mastodon/",
        views.connect_mastodon,
        name="connect_mastodon",
    ),
    path(
        "<uuid:workspace_id>/connect/devto/",
        views.connect_devto,
        name="connect_devto",
    ),
    path(
        "<uuid:workspace_id>/connect/whatsapp/",
        views.connect_whatsapp,
        name="connect_whatsapp",
    ),
    # OAuth callback (not workspace-scoped - platform redirects here)
    path(
        "callback/<str:platform>/",
        views.oauth_callback,
        name="oauth_callback",
    ),
    # Account selection (Facebook multi-page)
    path(
        "select-account/",
        views.select_account,
        name="select_account",
    ),
    # Per-account actions
    path(
        "<uuid:workspace_id>/<uuid:account_id>/reconnect/",
        views.reconnect,
        name="reconnect",
    ),
    path(
        "<uuid:workspace_id>/<uuid:account_id>/retry-webhooks/",
        views.retry_webhooks,
        name="retry_webhooks",
    ),
    path(
        "<uuid:workspace_id>/<uuid:account_id>/import-inbox-history/",
        views.import_inbox_history,
        name="import_inbox_history",
    ),
    path(
        "<uuid:workspace_id>/<uuid:account_id>/clear-inbox-history/",
        views.clear_inbox_history,
        name="clear_inbox_history",
    ),
    path(
        "<uuid:workspace_id>/<uuid:account_id>/card/",
        views.account_card,
        name="account_card",
    ),
    path(
        "<uuid:workspace_id>/<uuid:account_id>/whatsapp-templates/",
        views.whatsapp_templates,
        name="whatsapp_templates",
    ),
    path(
        "<uuid:workspace_id>/<uuid:account_id>/disconnect/",
        views.disconnect,
        name="disconnect",
    ),
]
