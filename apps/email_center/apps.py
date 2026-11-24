from django.apps import AppConfig


class EmailCenterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.email_center"
    verbose_name = "E-mail centre"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task

        from .tasks import AUTOMATED_EMAILS_INTERVAL_SECONDS, send_automated_emails

        register_recurring_task(
            send_automated_emails,
            repeat=AUTOMATED_EMAILS_INTERVAL_SECONDS,
            verbose_name="send_automated_emails",
        )
