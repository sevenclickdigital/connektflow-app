from django.apps import AppConfig


class SupportConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.support"
    verbose_name = "Support"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task

        from .tasks import ABANDONED_CHATS_INTERVAL_SECONDS, convert_abandoned_chats

        register_recurring_task(
            convert_abandoned_chats,
            repeat=ABANDONED_CHATS_INTERVAL_SECONDS,
            verbose_name="convert_abandoned_chats",
        )
