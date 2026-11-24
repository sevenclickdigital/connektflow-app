from django.apps import AppConfig


class ClientPortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.client_portal"
    verbose_name = "Client Portal"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in

        from .middleware import clear_portal_flags

        user_logged_in.connect(clear_portal_flags, dispatch_uid="client_portal.clear_portal_flags")
