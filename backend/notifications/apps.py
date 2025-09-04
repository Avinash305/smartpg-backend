from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notifications'

    def ready(self):
        # Import signal receivers
        try:
            from . import receivers  # noqa: F401
        except Exception:
            # Avoid hard crash if import-time dependencies are missing during migrations
            pass
