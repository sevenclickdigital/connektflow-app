from django.urls import path

from . import views

app_name = "organizations"

urlpatterns = [
    path("settings/", views.settings_view, name="settings"),
    path("workspaces/", views.workspaces_view, name="workspaces"),
    path("rename/", views.rename_view, name="rename"),
    path("edit/", views.edit_view, name="edit"),
    path("create/", views.create_view, name="create"),
    path("move-brand/", views.move_brand_view, name="move_brand"),
    path("<uuid:organization_id>/roles/", views.role_permissions_view, name="role_permissions"),
    path("<uuid:organization_id>/", views.settings_view, name="manage"),
    path("welcome/", views.welcome_view, name="welcome"),
    path("about/", views.about_view, name="about"),
    path("calendar/", views.cross_workspace_calendar, name="cross_workspace_calendar"),
]
