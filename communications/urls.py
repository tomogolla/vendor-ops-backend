"""Webhook URLs, mounted at /api/v1/webhooks/anymail/ with Anymail's own paths and URL names."""

from django.urls import path

from .webhooks import SignedResendTrackingWebhookView

app_name = 'anymail'
urlpatterns = [
    path('resend/tracking/', SignedResendTrackingWebhookView.as_view(), name='resend_tracking_webhook'),
]
