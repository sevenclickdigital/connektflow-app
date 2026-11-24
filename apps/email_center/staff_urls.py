"""Staff-only URLs of the e-mail centre, mounted under ``/staff/emails/``.

A separate namespace, like the other staff areas: these paths carry no
``workspace_id``. Access is enforced in the views (superadmins only).
"""

from django.urls import path

from . import staff_views

app_name = "email_center_staff"

urlpatterns = [
    path("", staff_views.index, name="index"),
    path("preview/", staff_views.preview, name="preview"),
    path("send-test/", staff_views.send_test, name="send_test"),
    path("system/<path:key>/reset/", staff_views.system_reset, name="system_reset"),
    path("system/<path:key>/", staff_views.system_edit, name="system_edit"),
    path("automatic/new/", staff_views.automated_edit, name="automated_new"),
    path("automatic/<uuid:email_id>/", staff_views.automated_edit, name="automated_edit"),
    path("automatic/<uuid:email_id>/toggle/", staff_views.automated_toggle, name="automated_toggle"),
    path("automatic/<uuid:email_id>/delete/", staff_views.automated_delete, name="automated_delete"),
]
