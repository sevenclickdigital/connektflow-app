"""URL patterns of the API key management UI.

Mounted at ``/organizations/api-keys/`` by ``config/urls.py``: the
organization-wide list and the endpoints that issue, edit and revoke a key.
The page the menus lead to is the brand's own list, the same ``list_keys``
view mounted at ``workspaces:api_keys``.
"""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "api_keys"

urlpatterns = [
    path("", views.list_keys, name="list"),
    path("issue/", views.issue_key, name="issue"),
    path("<uuid:key_id>/revoke/", views.revoke_key, name="revoke"),
    path("<uuid:key_id>/edit/", views.edit_key, name="edit"),
    # HTMX partial — cascades the workspace dropdown into a list of
    # connected SocialAccounts + the permission catalog grantable to
    # the issuer in that workspace.
    path("_workspace-options/", views.workspace_options_partial, name="workspace_options"),
]
