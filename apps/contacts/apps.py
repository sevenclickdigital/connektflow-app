from django.apps import AppConfig


class ContactsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.contacts"
    verbose_name = "Contacts"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task
        from apps.contacts.tasks import REFRESH_INTERVAL_SECONDS, refresh_contacts

        register_recurring_task(refresh_contacts, repeat=REFRESH_INTERVAL_SECONDS, verbose_name="refresh_contacts")
