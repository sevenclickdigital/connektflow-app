from django.apps import AppConfig
from django.db.models.signals import post_migrate


class RadarConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.radar"
    verbose_name = "Radar"

    def ready(self):
        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task
        from apps.radar.tasks import MARKET_HASHTAGS_INTERVAL_SECONDS, collect_market_hashtags

        register_recurring_task(
            collect_market_hashtags,
            repeat=MARKET_HASHTAGS_INTERVAL_SECONDS,
            verbose_name="collect_market_hashtags",
        )
