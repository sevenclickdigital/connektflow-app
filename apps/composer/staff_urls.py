"""Staff-only URLs for the shelf of idea templates.

A separate module and namespace from ``urls.py`` for the same reason
``apps.billing.staff_urls`` is: these paths carry no ``workspace_id``.
Access is enforced in the views.
"""

from django.urls import path

from . import staff_views

app_name = "composer_staff"

urlpatterns = [
    path("", staff_views.template_list, name="idea_templates"),
    path("new/", staff_views.template_edit, name="idea_template_create"),
    path("<int:template_id>/", staff_views.template_edit, name="idea_template_edit"),
    path("<int:template_id>/toggle/", staff_views.template_toggle, name="idea_template_toggle"),
    path("<int:template_id>/delete/", staff_views.template_delete, name="idea_template_delete"),
    path("<int:template_id>/review/", staff_views.template_review, name="idea_template_review"),
]
