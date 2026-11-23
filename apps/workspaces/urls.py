from django.urls import path

from apps.api_keys import views as api_key_views

from . import views

app_name = "workspaces"

urlpatterns = [
    path("", views.workspace_list, name="list"),
    path("create/", views.workspace_create, name="create"),
    path("<uuid:workspace_id>/dashboard/", views.workspace_dashboard, name="dashboard"),
    path("<uuid:workspace_id>/dashboard/goal/", views.dashboard_goal, name="dashboard_goal"),
    path("<uuid:workspace_id>/settings/", views.workspace_settings, name="settings"),
    path("<uuid:workspace_id>/settings/approvals/", views.approvals_settings, name="approvals_settings"),
    path("<uuid:workspace_id>/settings/team/", views.workspace_team, name="team"),
    path("<uuid:workspace_id>/settings/api-keys/", api_key_views.list_keys, name="api_keys"),
]
