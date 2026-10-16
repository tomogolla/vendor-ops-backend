"""Delivery tracking from Resend, through django-anymail's webhook.

POST /api/v1/webhooks/anymail/resend/tracking/ (Anymail's URL layout). Every call must carry a
valid Svix signature made with EMAIL_RESEND_WEBHOOK_SECRET: a bad signature is answered 400, and
while no secret is configured every call is refused. Only Resend's tracking view is mounted, so
no other provider's unsigned webhook is exposed.

Delivered, bounced and complained events update the matching EmailMessage (found by Resend's
message id). Hard bounces and complaints suppress the address in EmailRecipientPreference.
"""

import logging

from anymail.exceptions import AnymailWebhookValidationFailure
from anymail.signals import EventType, RejectReason, tracking
from anymail.webhooks.resend import ResendTrackingWebhookView
from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone

from vendors.models import ActivityEvent

from .models import EmailMessage, EmailRecipientPreference
from .services import _activity

logger = logging.getLogger('communications.webhooks')
Event = ActivityEvent.EventType
Status = EmailMessage.Status
Reason = EmailRecipientPreference.SuppressionReason


class SignedResendTrackingWebhookView(ResendTrackingWebhookView):
    """Anymail's Resend tracking view, refusing every call when no signing secret is configured
    (Anymail on its own would only warn and accept unsigned calls)."""

    def validate_request(self, request):
        if self._svix_webhook is None:
            raise AnymailWebhookValidationFailure(
                'Resend webhook signing secret is not configured (EMAIL_RESEND_WEBHOOK_SECRET); refusing the call.'
            )


def suppress_address(email: str, reason: str, *, vendor=None, source: str):
    """Suppresses an address unless it already is (the first reason is kept)."""
    address = (email or '').strip().lower()
    if not address:
        return
    preference, _ = EmailRecipientPreference.objects.get_or_create(email=address, defaults={'vendor': vendor})
    if preference.suppressed:
        return
    preference.suppressed = True
    preference.suppression_reason = reason
    preference.source = source
    if preference.vendor_id is None:
        preference.vendor = vendor
    preference.save()


def is_hard_bounce(event) -> bool:
    bounce = ((event.esp_event or {}).get('data') or {}).get('bounce') or {}
    if str(bounce.get('type', '')).lower() == 'permanent':
        return True
    # Resend has already suppressed the address, or says so in words.
    return event.reject_reason == RejectReason.BLOCKED or 'hard bounce' in (event.description or '').lower()


@transaction.atomic
def apply_tracking_event(event) -> str:
    """Applies one Resend tracking event. Returns what happened (for logs and tests)."""
    if event.event_type not in (EventType.DELIVERED, EventType.BOUNCED, EventType.COMPLAINED):
        return 'ignored'
    message = (
        EmailMessage.objects.select_for_update()
        .filter(provider='resend', provider_message_id=str(event.message_id))
        .first()
    )
    if message is None:
        logger.info('Resend %s event for unknown message %s', event.event_type, event.message_id)
        return 'unknown_message'
    when = event.timestamp or timezone.now()
    source = 'resend webhook'

    if event.event_type == EventType.DELIVERED:
        if message.status != Status.SENT:
            return 'unchanged'  # already bounced, complained or delivered
        message.status = Status.DELIVERED
        message.delivered_at = when
        message.save(update_fields=['status', 'delivered_at', 'updated_at'])
        return 'delivered'

    if event.event_type == EventType.BOUNCED:
        if message.status not in (Status.SENT, Status.DELIVERED):
            return 'unchanged'
        hard = is_hard_bounce(event)
        message.status = Status.BOUNCED
        message.failure_code = 'hard_bounce' if hard else 'soft_bounce'
        message.failure_detail = (event.description or 'Resend reported a bounce.')[:500]
        message.save(update_fields=['status', 'failure_code', 'failure_detail', 'updated_at'])
        _activity(message, Event.EMAIL_BOUNCED, f'Email "{message.template_key}" bounced', reason=message.failure_code)
        if hard:
            _suppress_for(message, Reason.HARD_BOUNCE, source)
        return 'bounced'

    # Complained: the recipient marked it as spam.
    if message.status not in (Status.SENT, Status.DELIVERED, Status.BOUNCED):
        return 'unchanged'
    message.status = Status.COMPLAINED
    message.failure_code = 'complaint'
    message.failure_detail = 'The recipient marked the message as spam.'
    message.save(update_fields=['status', 'failure_code', 'failure_detail', 'updated_at'])
    _activity(message, Event.EMAIL_COMPLAINED, f'Email "{message.template_key}" marked as spam', reason='complaint')
    _suppress_for(message, Reason.COMPLAINT, source)
    return 'complained'


def _suppress_for(message: EmailMessage, reason: str, source: str):
    if message.redirected:
        # The event concerns the team redirect mailbox, not the vendor; never suppress the vendor for it.
        logger.warning('Resend %s for redirected message %s; vendor address not suppressed.', reason, message.pk)
        return
    suppress_address(message.delivered_to_email or message.recipient_email, reason, vendor=message.vendor, source=source)


@receiver(tracking, dispatch_uid='communications.resend_tracking')
def handle_tracking(sender, event, esp_name, **kwargs):
    if esp_name != 'Resend' or event.message_id in (None, ''):
        return
    outcome = apply_tracking_event(event)
    logger.info('Resend %s event for %s: %s', event.event_type, event.message_id, outcome)
