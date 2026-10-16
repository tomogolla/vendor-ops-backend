from django.apps import AppConfig


class CommunicationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'communications'
    verbose_name = 'Email communications'

    def ready(self):
        # Registers system checks, the delivery-tracking receiver and the application stop conditions.
        from . import application_emails, checks, webhooks  # noqa: F401
