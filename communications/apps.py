from django.apps import AppConfig


class CommunicationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'communications'
    verbose_name = 'Email communications'

    def ready(self):
        from . import checks, webhooks  # noqa: F401  (registers system checks and the tracking receiver)
