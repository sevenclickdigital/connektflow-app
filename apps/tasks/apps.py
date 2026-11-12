from django.apps import AppConfig


class TasksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tasks"
    verbose_name = "Tasks"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task
        from apps.tasks.tasks import DUE_REMINDER_INTERVAL_SECONDS, send_task_due_reminders

        register_recurring_task(
            send_task_due_reminders,
            repeat=DUE_REMINDER_INTERVAL_SECONDS,
            verbose_name="send_task_due_reminders",
        )
