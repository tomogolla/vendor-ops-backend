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
  expired, so re-enabling sending never delivers a stale backlog;
- a provider rate-limit reply (HTTP 429) puts the message and the rest of the batch back in the
  queue for EMAIL_RATE_LIMIT_DELAY_MINUTES without using up an attempt.

EMAIL_DAILY_LIMIT caps messages sent since local midnight in the business time zone (0 = no
limit). Once it is reached, the remaining messages simply stay queued for the next day.
"""

import html
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.validators import validate_email
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


def deliver(message: EmailMessage, provider: EmailProvider, config: EmailSettings, summary: Summary, now,
            *, allow_retry: bool = True) -> str:
    """Sends one claimed message. Returns 'sent', 'failed', 'retrying' or 'rate_limited'.
    With allow_retry=False (one-off test emails) every failure is final."""
    email = outbound_for(message, config)
    try:
        result = provider.send(email)
    except EmailSendError as error:
        if error.retry_later and allow_retry:
            release(message, now, error.code, error.detail)
            summary.deferred += 1
            return 'rate_limited'
        return _record_failure(message, error.code, error.detail, transient=error.transient and allow_retry,
                               summary=summary, now=now)
    except Exception as error:  # noqa: BLE001 - one bad message must not stop the batch
        logger.exception('Unexpected error sending email %s', message.pk)
        return _record_failure(message, 'provider_error', f'Unexpected {type(error).__name__}.', transient=allow_retry,
                               summary=summary, now=now)
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
    return 'sent'


def release(message: EmailMessage, now, code: str = 'rate_limited', detail: str = ''):
    """Back to the queue without using up an attempt (provider rate limit)."""
    message.status = Status.QUEUED
    message.attempt_count = max(message.attempt_count - 1, 0)
    message.scheduled_for = now + timedelta(minutes=settings.EMAIL_RATE_LIMIT_DELAY_MINUTES)
    message.failure_code = code
    message.failure_detail = (detail or 'Provider rate limit; will try later.')[:500]
    message.save(update_fields=['status', 'attempt_count', 'scheduled_for', 'failure_code', 'failure_detail', 'updated_at'])


def local_midnight(now, config: EmailSettings) -> datetime:
    local = now.astimezone(ZoneInfo(config.business_timezone))
    return datetime.combine(local.date(), time(0), tzinfo=local.tzinfo)


def daily_remaining(now, config: EmailSettings) -> int | None:
    """Messages that may still be sent today, or None when there is no daily limit. Counts sends
    since local midnight in the business time zone, plus messages being sent right now."""
    limit = settings.EMAIL_DAILY_LIMIT
    if not limit:
        return None
    used = EmailMessage.objects.filter(sent_at__gte=local_midnight(now, config)).count()
    used += EmailMessage.objects.filter(status=Status.SENDING).count()
    return max(limit - used, 0)


def _record_failure(message: EmailMessage, code: str, detail: str, *, transient: bool, summary: Summary, now) -> str:
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
        return 'retrying'
    message.status = Status.FAILED
    message.save(update_fields=['status', 'failure_code', 'failure_detail', 'provider', 'updated_at'])
    _activity(message, Event.EMAIL_FAILED, f'Email "{message.template_key}" failed: {detail}', reason=code,
              attempts=message.attempt_count)
    summary.failed += 1
    return 'failed'


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

    remaining = daily_remaining(now, config)
    if remaining == 0:
        summary.deferred = due.count()
        summary.note = _limit_note(summary.deferred, config)
        return summary
    batch_size = limit if remaining is None else min(limit, remaining)

    if dry_run:
        for message in due.order_by('scheduled_for', 'created_at')[:batch_size]:
            summary.scanned += 1
            if preflight(message, now, config):
                summary.skipped += 1
            else:
                summary.sent += 1
        summary.note = f'would send {summary.sent} in {mode} mode; nothing was changed'
        if remaining is not None and due.count() > batch_size:
            summary.note += f'; {_limit_note(due.count() - summary.scanned, config)}'
        return summary

    provider = provider or get_provider()
    claimed = claim(now, batch_size, config, summary)
    notes = ['redirect mode'] if mode == EmailSettings.SendingMode.REDIRECT else []
    for index, message in enumerate(claimed):
        if deliver(message, provider, config, summary, now) == 'rate_limited':
            for waiting in claimed[index + 1:]:
                release(waiting, now)
                summary.deferred += 1
            notes.append('provider rate limit (HTTP 429): the rest will be retried shortly')
            break
    if remaining is not None and daily_remaining(now, config) == 0:
        left = due.count()
        if left:
            summary.deferred += left
            notes.append(_limit_note(left, config))
    summary.note = '; '.join(notes)
    return summary


def _limit_note(left: int, config: EmailSettings) -> str:
    return (f'daily limit of {settings.EMAIL_DAILY_LIMIT} reached (since midnight {config.business_timezone}); '
            f'{left} left queued')


# --- One-off test email ----------------------------------------------------------------------

@dataclass
class TestEmailResult:
    """`sent`, `failed`, `disabled`, `no_redirect_address`, `daily_limit`, `suppressed`, `missing_sender`."""
    outcome: str
    detail: str
    message: EmailMessage | None = None


def send_test_email(address: str, *, now=None, provider: EmailProvider | None = None) -> TestEmailResult:
    """Sends one test message through the real path (EmailMessage record, mode, redirect, provider,
    daily limit), without the send window and without retries."""
    address = (address or '').strip()
    validate_email(address)  # raises django.core.exceptions.ValidationError
    now = now or timezone.now()
    config = EmailSettings.load()
    mode = config.effective_mode
    if mode == EmailSettings.SendingMode.DISABLED:
        return TestEmailResult('disabled', 'Sending is disabled (EMAIL_GLOBAL_MODE or the admin email settings); nothing was sent.')
    if mode == EmailSettings.SendingMode.REDIRECT and not config.effective_redirect_to:
        return TestEmailResult('no_redirect_address', 'Redirect mode is on but no redirect address is configured; nothing was sent.')
    if not config.effective_sender_email:
        return TestEmailResult('missing_sender', 'No sender address is configured (EMAIL_FROM_ADDRESS); nothing was sent.')
    if daily_remaining(now, config) == 0:
        return TestEmailResult('daily_limit', f'The daily limit of {settings.EMAIL_DAILY_LIMIT} is reached; nothing was sent.')
    problem = recipient_problem(address, EmailTemplate.Category.TRANSACTIONAL)
    if problem:
        return TestEmailResult('suppressed', f'{problem[1]} Nothing was sent.')

    stamp = now.astimezone(ZoneInfo(config.business_timezone)).strftime('%Y-%m-%d %H:%M %Z')
    text = (f'This is a test email from Vendor Ops, sent {stamp} through the "{settings.EMAIL_PROVIDER}" provider '
            f'in {mode} mode. No action is needed.')
    message = EmailMessage.objects.create(
        template_key='test_email', recipient_email=address, sender_name=config.effective_sender_name,
        sender_email=config.effective_sender_email, reply_to_email=config.effective_reply_to,
        subject='Vendor Ops test email', html_body=f'<p>{html.escape(text)}</p>', text_body=text,
        status=Status.SENDING, scheduled_for=now, attempt_count=1, last_attempt_at=now,
        event_key=f'test-email:{now.isoformat()}',
    )
    outcome = deliver(message, provider or get_provider(), config, Summary(), now, allow_retry=False)
    message.refresh_from_db()
    if outcome == 'sent':
        where = (f'the redirect mailbox {message.delivered_to_email} instead of {address}'
                 if message.redirected else message.delivered_to_email)
        return TestEmailResult('sent', f'Sent to {where} via {message.provider} (provider message id '
                                       f'{message.provider_message_id}).', message)
    return TestEmailResult('failed', f'Not sent: {message.failure_code}: {message.failure_detail}', message)
