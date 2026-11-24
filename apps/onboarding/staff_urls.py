"""Staff-only URLs for the pages' tutorials (video and text).

A separate module and namespace from ``urls.py`` for the same reason
``apps.billing.staff_urls`` is: these paths carry no ``workspace_id``.
Access is enforced in the views.
"""

from django.urls import path

from . import staff_views

app_name = "onboarding_staff"

urlpatterns = [
    path("", staff_views.help_videos_list, name="help_videos"),
    path("save/", staff_views.help_video_save, name="help_video_save"),
    path("<slug:page>/", staff_views.tutorial_edit, name="tutorial_edit"),
]
