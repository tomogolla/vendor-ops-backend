"""The delivery worker behind `manage.py send_due_emails`.

Safe to run repeatedly and concurrently: due messages are claimed with
SELECT ... FOR UPDATE SKIP LOCKED and marked `sending` before the transport is called, so two
runs never pick the same message. Retry policy (conservative, explicit):

- transient failures (connection problems, 4xx replies) are retried at most
  EMAIL_MAX_ATTEMPTS times, after EMAIL_RETRY_DELAYS_MINUTES, then marked failed;
- permanent failures (5xx replies, refused recipients, authentication) fail at once;
- a message left in `sending` for EMAIL_STUCK_SENDING_MINUTES (the worker died mid-send) is
  marked failed, never re-sent, because it may already have been delivered;
- a queued message more than EMAIL_QUEUE_MAX_AGE_HOURS past its scheduled time is skipped as
  expired, so re-enabling sending never delivers a stale backlog.
"""

import html
import logging
from dataclasses import asdict, dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from vendors.models import ActivityEvent

from . import stop_conditions
from .models import EmailMessage, EmailSettings, EmailTemplate
from .providers import EmailProvider, EmailSendError, OutboundEmail, get_provider
from .services import _activity, recipient_problem
from .windows import in_window

logger = logging.getLogger('communications.sending')
Event = ActivityEvent.EventType
Status = EmailMessage.Status


@dataclass
class Summary:
    scanned: int = 0
    sent: int = 0
    drafted: int = 0
    skipped: int = 0
    failed: int = 0
    deferred: int = 0
    dry_run: bool = False
    note: str = ''

    def line(self) -> str:
        counts = ' '.join(f'{key}={value}' for key, value in asdict(self).items() if key not in ('dry_run', 'note'))
        prefix = '[dry run] ' if self.dry_run else ''
        return f'{prefix}send_due_emails: {counts}{f" ({self.note})" if self.note else ""}'


def _skip(message: EmailMessage, code: str, detail: str):
    message.status = Status.SKIPPED
    message.failure_code = code
    message.failure_detail = detail[:500]
    message.save(update_fields=['status', 'failure_code', 'failure_detail', 'updated_at'])
    _activity(message, Event.EMAIL_SKIPPED, f'Email "{message.template_key}" skipped before sending: {detail}', reason=code)


def preflight(message: EmailMessage, now, config: EmailSettings) -> tuple[str, str] | None:
    """Re-checked immediately before sending: age, sender, recipient, suppression, stop conditions."""
    max_age = timedelta(hours=settings.EMAIL_QUEUE_MAX_AGE_HOURS)
    if message.scheduled_for and message.scheduled_for < now - max_age:
        return 'expired', f'Not sent within {settings.EMAIL_QUEUE_MAX_AGE_HOURS} hours of its scheduled time.'
    if not message.sender_email:
        return 'missing_sender', 'No sender address was configured when the message was queued.'
    category = message.template.category if message.template else EmailTemplate.Category.TRANSACTIONAL
    problem = recipient_problem(message.recipient_email, category)
    if problem:
        return problem
    stop = stop_conditions.stop_reason(message)
    if stop:
        return 'stop_condition', f'{stop[0]}: {stop[1]}'
    return None


def outbound_for(message: EmailMessage, config: EmailSettings) -> OutboundEmail:
    """The message as it goes on the wire. In redirect mode the vendor never receives it: it goes to
    the redirect mailbox, marked with the original recipient."""
    headers = {'X-Vendor-Ops-Message': str(message.pk)}
    if config.effective_mode == EmailSettings.SendingMode.REDIRECT:
        original = f'{message.recipient_name} <{message.recipient_email}>'
        headers['X-Vendor-Ops-Original-Recipient'] = message.recipient_email
        return OutboundEmail(
            to_name='Vendor Ops redirect', to_email=config.effective_redirect_to,
            from_name=message.sender_name, from_email=message.sender_email, reply_to=message.reply_to_email,
            subject=f'[Redirected from {message.recipient_email}] {message.subject}'[:255],
            html_body=(
                f'<p style="padding:8px;background:#fff3cd"><strong>Redirect mode.</strong> '
                f'Original recipient: {html.escape(original)}</p>{message.html_body}'
            ),
            text_body=f'[Redirect mode. Original recipient: {original}]\n\n{message.text_body}',
            headers=headers,
        )
    return OutboundEmail(
        to_name=message.recipient_name, to_email=message.recipient_email,
        from_name=message.sender_name, from_email=message.sender_email, reply_to=message.reply_to_email,
        subject=message.subject, html_body=message.html_body, text_body=message.text_body, headers=headers,
    )


def recover_stuck(now, summary: Summary):
    cutoff = now - timedelta(minutes=settings.EMAIL_STUCK_SENDING_MINUTES)
    with transaction.atomic():
        stuck = EmailMessage.objects.select_for_update(skip_locked=True).filter(
            status=Status.SENDING, last_attempt_at__lt=cutoff,
        )
        for message in stuck:
            message.status = Status.FAILED
            message.failure_code = 'stuck_sending'
            message.failure_detail = (
                'The worker stopped while sending. Not retried, because it may have been delivered; '
                'check the mailbox before sending again.'
            )
            message.save(update_fields=['status', 'failure_code', 'failure_detail', 'updated_at'])
            _activity(message, Event.EMAIL_FAILED, f'Email "{message.template_key}" failed: worker stopped while sending',
                      reason='stuck_sending')
            summary.failed += 1


def claim(now, limit: int, config: EmailSettings, summary: Summary) -> list[EmailMessage]:
    """Locks a batch of due messages, skips the ones that must not go, marks the rest `sending`."""
    claimed: list[EmailMessage] = []
    with transaction.atomic():
        batch = list(
            EmailMessage.objects.select_for_update(skip_locked=True)
            .filter(status=Status.QUEUED, scheduled_for__lte=now)
            .order_by('scheduled_for', 'created_at')[:limit]
        )
        for message in batch:
            summary.scanned += 1
            problem = preflight(message, now, config)
            if problem:
                _skip(message, *problem)
                summary.skipped += 1
                continue
            message.status = Status.SENDING
            message.attempt_count += 1
            message.last_attempt_at = now
            message.save(update_fields=['status', 'attempt_count', 'last_attempt_at', 'updated_at'])
            claimed.append(message)
    return claimed


def deliver(message: EmailMessage, provider: EmailProvider, config: EmailSettings, summary: Summary, now):
    email = outbound_for(message, config)
    try:
        result = provider.send(email)
    except EmailSendError as error:
        _record_failure(message, error.code, error.detail, transient=error.transient, summary=summary, now=now)
        return
    except Exception as error:  # noqa: BLE001 - one bad message must not stop the batch
        logger.exception('Unexpected error sending email %s', message.pk)
        _record_failure(message, 'provider_error', f'Unexpected {type(error).__name__}.', transient=True, summary=summary, now=now)
        return
    message.status = Status.SENT
    message.sent_at = timezone.now()
    message.provider = result.provider
    message.provider_message_id = result.message_id[:255]
    message.delivered_to_email = email.to_email
    message.redirected = config.effective_mode == EmailSettings.SendingMode.REDIRECT
    message.failure_code = ''
    message.failure_detail = ''
    message.save(update_fields=[
        'status', 'sent_at', 'provider', 'provider_message_id', 'delivered_to_email', 'redirected',
        'failure_code', 'failure_detail', 'updated_at',
    ])
    _activity(message, Event.EMAIL_SENT,
              f'Email "{message.template_key}" sent{" (redirected to the team mailbox)" if message.redirected else ""}',
              redirected=message.redirected, provider=result.provider)
    summary.sent += 1


def _record_failure(message: EmailMessage, code: str, detail: str, *, transient: bool, summary: Summary, now):
    message.failure_code = code
    message.failure_detail = detail[:500]
    message.provider = settings.EMAIL_PROVIDER
    delays = settings.EMAIL_RETRY_DELAYS_MINUTES
    if transient and message.attempt_count < settings.EMAIL_MAX_ATTEMPTS:
        delay = delays[min(message.attempt_count - 1, len(delays) - 1)]
        message.status = Status.QUEUED
        message.scheduled_for = now + timedelta(minutes=delay)
        message.save(update_fields=['status', 'scheduled_for', 'failure_code', 'failure_detail', 'provider', 'updated_at'])
        logger.warning('Email %s attempt %s failed (%s); retrying in %s minutes', message.pk, message.attempt_count, code, delay)
        summary.deferred += 1
        return
    message.status = Status.FAILED
    message.save(update_fields=['status', 'failure_code', 'failure_detail', 'provider', 'updated_at'])
    _activity(message, Event.EMAIL_FAILED, f'Email "{message.template_key}" failed: {detail}', reason=code,
              attempts=message.attempt_count)
    summary.failed += 1


def process_due_emails(*, limit: int = 50, dry_run: bool = False, now=None, provider: EmailProvider | None = None) -> Summary:
    now = now or timezone.now()
    summary = Summary(dry_run=dry_run)
    config = EmailSettings.load()
    summary.drafted = EmailMessage.objects.filter(status=Status.DRAFT).count()
    due = EmailMessage.objects.filter(status=Status.QUEUED, scheduled_for__lte=now)

    if not dry_run:
        recover_stuck(now, summary)

    mode = config.effective_mode
    if mode == EmailSettings.SendingMode.DISABLED:
        summary.deferred = due.count()
        summary.note = 'sending is disabled'
        return summary
    if mode == EmailSettings.SendingMode.REDIRECT and not config.effective_redirect_to:
        summary.deferred = due.count()
        summary.note = 'redirect mode has no redirect address; nothing sent'
        logger.error('Email redirect mode is on but no redirect address is configured.')
        return summary
    if not in_window(now, config.business_timezone, config.send_window_start, config.send_window_end):
        summary.deferred = due.count()
        summary.note = 'outside the send window'
        return summary

    if dry_run:
        for message in due.order_by('scheduled_for', 'created_at')[:limit]:
            summary.scanned += 1
            if preflight(message, now, config):
                summary.skipped += 1
            else:
                summary.sent += 1
        summary.note = f'would send {summary.sent} in {mode} mode; nothing was changed'
        return summary

    provider = provider or get_provider()
    for message in claim(now, limit, config, summary):
        deliver(message, provider, config, summary, now)
    if mode == EmailSettings.SendingMode.REDIRECT:
        summary.note = 'redirect mode'
    return summary
