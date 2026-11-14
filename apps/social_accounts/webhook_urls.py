"""Platform callback URL patterns - not auth-protected, CSRF-exempt."""

from django.urls import path

from . import platform_callbacks

urlpatterns = [
    path("threads/deauthorize/", platform_callbacks.threads_deauthorize, name="threads_deauthorize"),
    path("threads/data-deletion/", platform_callbacks.threads_data_deletion, name="threads_data_deletion"),
]
