from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.common"
    verbose_name = "Common"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task
        from apps.common.tasks import (
            COMPLETED_TASK_PURGE_INTERVAL_SECONDS,
            COUNTER_PURGE_INTERVAL_SECONDS,
            purge_completed_tasks,
            purge_email_counters,
        )

        register_recurring_task(
            purge_email_counters,
            repeat=COUNTER_PURGE_INTERVAL_SECONDS,
            verbose_name="purge_email_counters",
        )
        register_recurring_task(
            purge_completed_tasks,
            repeat=COMPLETED_TASK_PURGE_INTERVAL_SECONDS,
            verbose_name="purge_completed_tasks",
        )
