from django.urls import path

from . import views

app_name = "contacts"

urlpatterns = [
    path("", views.contact_list, name="list"),
    path("new/", views.contact_create, name="create"),
    path("export/", views.contact_export, name="export"),
    path("import/", views.contact_import, name="import"),
    path("bulk-delete/", views.contact_bulk_delete, name="bulk_delete"),
    path("merge/", views.contact_merge, name="merge"),
    path("<uuid:contact_id>/", views.contact_edit, name="edit"),
    path("<uuid:contact_id>/delete/", views.contact_delete, name="delete"),
    path("<uuid:contact_id>/funnel/", views.contact_set_funnel, name="set_funnel"),
]
