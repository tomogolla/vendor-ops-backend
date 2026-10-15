"""Queuing and controlled status changes for email. Nothing here talks to a mail server: delivery
happens only in the `send_due_emails` worker (sending.py).

Workflow services call `queue_email_on_commit(...)` so a message is only created once their
transaction has committed. Slice 1 wires no triggers yet.
"""

import hashlib
import logging
from dataclasses import dataclass
from functools import partial
from urllib.parse import urlparse

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts.permissions import is_admin
from tasks.models import Task
from tasks.scheduling import discovery_call_due_at
from vendors.activity import record_activity
from vendors.models import ActivityEvent

from .models import EmailMessage, EmailRecipientPreference, EmailSettings, EmailTemplate
from .rendering import VARIABLES, MissingVariableError, TemplateSyntaxError, render
from .windows import next_send_time

logger = logging.getLogger(__name__)
Event = ActivityEvent.EventType
Status = EmailMessage.Status

# Variables only a caller can supply (everything else comes from the vendor, application or weekend).
CALLER_VARIABLES = {'payment_link'}


@dataclass
class QueueResult:
    """`off` (template off or inactive: nothing created), `duplicate` (already queued for this
    event), `rejected` (could not render: nothing created), or the new message's status:
    `queued`, `draft`, `skipped`."""
    outcome: str
    message: EmailMessage | None = None
    reason: str = ''


# --- Context -----------------------------------------------------------------------------

def application_reference(application) -> str:
    return f'APP-{str(application.pk).replace("-", "")[:8].upper()}'


def market_dates(weekend) -> str:
    start, end = weekend.start_date, weekend.end_date
    if start == end:
        return f'{start:%b} {start.day}, {start.year}'
    if (start.year, start.month) == (end.year, end.month):
        return f'{start:%b} {start.day} – {end.day}, {end.year}'
    return f'{start:%b} {start.day} – {end:%b} {end.day}, {end.year}'


def build_context(vendor, application=None, market_weekend=None, extra: dict | None = None) -> dict[str, str]:
    """The only values a template can see: derived from the records, plus whitelisted caller values."""
    owner = vendor.owner
    values = {
        'vendor_name': vendor.brand_name,
        'owner_name': (owner.get_full_name() or owner.username) if owner else 'The Good Flea team',
    }
    if application is not None:
        values['application_reference'] = application_reference(application)
    if market_weekend is not None:
        values['market_name'] = market_weekend.label
        values['market_dates'] = market_dates(market_weekend)
    for key, value in (extra or {}).items():
        if key not in VARIABLES:
            raise ValueError(f'Unknown email variable {key!r}.')
        if key not in CALLER_VARIABLES:
            raise ValueError(f'{key!r} is derived from the records and cannot be passed in.')
        if key == 'payment_link':
            parsed = urlparse(str(value))
            if parsed.scheme != 'https' or not parsed.netloc:
                raise ValueError('payment_link must be an https URL.')
        values[key] = str(value)
    return values


def dedupe_key(template_key: str, *, vendor, application, event_key: str) -> str:
    subject = f'application:{application.pk}' if application is not None else f'vendor:{vendor.pk}'
    key = f'{template_key}|{subject}|{event_key}'
    return key if len(key) <= 255 else f'{key[:190]}|sha256:{hashlib.sha256(key.encode()).hexdigest()}'


# --- Recipient checks (shared with the worker) -----------------------------------------------

def recipient_problem(email: str, category: str) -> tuple[str, str] | None:
    """(code, detail) when this address must not receive this category of email, else None."""
    address = (email or '').strip()
    if not address:
        return 'missing_email', 'The vendor has no email address.'
    try:
        validate_email(address)
    except DjangoValidationError:
        return 'invalid_email', 'The vendor email address is not valid.'
    preference = EmailRecipientPreference.objects.filter(email=address.lower()).first()
    if preference and preference.suppressed:
        return 'suppressed', f'Address suppressed ({preference.get_suppression_reason_display()}).'
    if category == EmailTemplate.Category.PROMOTIONAL:
        if not preference or preference.marketing_consent != EmailRecipientPreference.Consent.GRANTED:
            return 'no_marketing_consent', 'No marketing consent for this address.'
    elif preference and not preference.transactional_permitted:
        return 'transactional_not_permitted', 'Transactional email is not permitted for this address.'
    return None


def _activity(message: EmailMessage, event_type: str, description: str, *, actor=None, **metadata):
    """Timeline entry for an email outcome. Never includes the body or the address."""
    if message.vendor_id is None:
        return
    record_activity(
        message.vendor, event_type, description, actor=actor, application=message.application,
        task=message.review_task, email_message_id=message.pk, template_key=message.template_key,
        status=message.status, **metadata,
    )


# --- Queuing -------------------------------------------------------------------------------

def queue_email(
    template_key: str, *, vendor, application=None, market_weekend=None, event_key: str,
    actor=None, context: dict | None = None,
) -> QueueResult:
    """Renders a template for the vendor contact and records the outcome. Call it through
    `queue_email_on_commit` from workflow services. Idempotent per template + application (or
    vendor) + event_key: a repeat call returns `duplicate` and never overwrites the first message."""
    if not event_key:
        raise ValueError('event_key is required.')
    template = EmailTemplate.objects.filter(key=template_key).first()
    if template is None:
        raise ValueError(f'No email template {template_key!r}.')
    if not template.is_active or template.mode == EmailTemplate.Mode.OFF:
        return QueueResult('off', reason='The template is off.')

    key = dedupe_key(template_key, vendor=vendor, application=application, event_key=event_key)
    existing = EmailMessage.objects.filter(dedupe_key=key).first()
    if existing:
        return QueueResult('duplicate', existing, 'Already created for this event.')

    try:
        values = build_context(vendor, application, market_weekend, context)
        subject = render(template.subject_template, values, escape_html=False).replace('\n', ' ').strip()
        html_body = render(template.html_template, values, escape_html=True)
        text_body = render(template.text_template, values, escape_html=False)
    except (MissingVariableError, TemplateSyntaxError) as error:
        logger.warning('Email %s for vendor %s not rendered: %s', template_key, vendor.pk, error)
        record_activity(
            vendor, Event.EMAIL_SKIPPED, f'Email "{template.name}" not created: {error}', actor=actor,
            application=application, template_key=template_key, reason='render_error', event_key=event_key,
        )
        return QueueResult('rejected', reason=str(error))

    config = EmailSettings.load()
    problem = recipient_problem(vendor.email, template.category)
    now = timezone.now()
    if problem:
        status, scheduled_for = Status.SKIPPED, None
    elif template.mode == EmailTemplate.Mode.DRAFT:
        status, scheduled_for = Status.DRAFT, None
    else:
        status = Status.QUEUED
        scheduled_for = next_send_time(now, config.business_timezone, config.send_window_start, config.send_window_end)

    try:
        with transaction.atomic():
            message = EmailMessage.objects.create(
                template=template, template_key=template.key, vendor=vendor, application=application,
                market_weekend=market_weekend,
                recipient_name=(f'{vendor.first_name} {vendor.last_name}'.strip() or vendor.brand_name),
                recipient_email=(vendor.email or '').strip(),
                sender_name=config.effective_sender_name, sender_email=config.effective_sender_email,
                reply_to_email=config.effective_reply_to,
                subject=subject[:255], html_body=html_body, text_body=text_body,
                status=status, scheduled_for=scheduled_for,
                failure_code=problem[0] if problem else '', failure_detail=problem[1] if problem else '',
                dedupe_key=key, event_key=event_key, created_by=actor,
            )
            if status == Status.DRAFT:
                message.review_task = _create_review_task(message, template, actor)
                message.save(update_fields=['review_task', 'updated_at'])
                _activity(message, Event.EMAIL_DRAFTED, f'Email "{template.name}" drafted for review', actor=actor)
            elif status == Status.QUEUED:
                _activity(message, Event.EMAIL_QUEUED, f'Email "{template.name}" queued', actor=actor,
                          scheduled_for=scheduled_for.isoformat())
            else:
                _activity(message, Event.EMAIL_SKIPPED, f'Email "{template.name}" skipped: {problem[1]}', actor=actor,
                          reason=problem[0])
    except IntegrityError:
        # Another process created the same event's message first.
        existing = EmailMessage.objects.filter(dedupe_key=key).first()
        if existing is None:
            raise
        return QueueResult('duplicate', existing, 'Already created for this event.')
    return QueueResult(status, message, problem[1] if problem else '')


def queue_email_on_commit(template_key: str, **kwargs) -> None:
    """Queues after the surrounding transaction commits (immediately when there is none), so an
    email can never describe a change that was rolled back."""
    transaction.on_commit(partial(queue_email, template_key, **kwargs))


def _create_review_task(message: EmailMessage, template: EmailTemplate, actor) -> Task:
    application = message.application
    owner = (application.reviewer if application is not None else None) or message.vendor.owner
    task = Task.objects.create(
        vendor=message.vendor, application=application, task_type=Task.TaskType.EMAIL_REVIEW,
        title=f'Review email: {template.name} to {message.vendor.brand_name}',
        priority=Task.Priority.NORMAL, owner=owner, due_at=discovery_call_due_at(), created_by=actor,
        description=(
            f'Review the drafted "{template.name}" email, then approve or cancel it in the admin '
            f'(/admin/communications/emailmessage/{message.pk}/change/).'
        ),
    )
    record_activity(
        message.vendor, Event.TASK_CREATED, f'Review email task created: {task.title}', actor=actor,
        application=application, task=task, task_type=task.task_type, owner=str(owner.pk) if owner else None,
        due_at=task.due_at.isoformat() if task.due_at else None,
    )
    return task


# --- Controlled status changes (administrators) ------------------------------------------------

def _close_review_task(message: EmailMessage, status: str, outcome: str):
    task = message.review_task
    if task is not None and task.is_open:
        task.status = status
        task.outcome = outcome
        task.completed_at = timezone.now() if status == Task.Status.COMPLETED else None
        task.save(update_fields=['status', 'outcome', 'completed_at', 'updated_at'])


@transaction.atomic
def approve_draft(message: EmailMessage, *, actor) -> EmailMessage:
    """A reviewed draft joins the queue for the next send window. Its content does not change."""
    if not is_admin(actor):
        raise PermissionDenied('Only administrators can approve emails.')
    message = EmailMessage.objects.select_for_update().get(pk=message.pk)
    if message.status != Status.DRAFT:
        raise ValidationError({'status': f'Only drafts can be approved (this one is {message.status}).'})
    config = EmailSettings.load()
    message.status = Status.QUEUED
    message.scheduled_for = next_send_time(
        timezone.now(), config.business_timezone, config.send_window_start, config.send_window_end
    )
    message.save(update_fields=['status', 'scheduled_for', 'updated_at'])
    _close_review_task(message, Task.Status.COMPLETED, 'Approved')
    _activity(message, Event.EMAIL_QUEUED, 'Drafted email approved and queued', actor=actor,
              scheduled_for=message.scheduled_for.isoformat(), approved_from_draft=True)
    return message


@transaction.atomic
def cancel_message(message: EmailMessage, *, actor, reason: str = '') -> EmailMessage:
    if not is_admin(actor):
        raise PermissionDenied('Only administrators can cancel emails.')
    message = EmailMessage.objects.select_for_update().get(pk=message.pk)
    if message.status not in (Status.DRAFT, Status.QUEUED):
        raise ValidationError({'status': f'Only drafts and queued emails can be cancelled (this one is {message.status}).'})
    message.status = Status.CANCELLED
    message.failure_code = 'cancelled'
    message.failure_detail = (reason or 'Cancelled by an administrator.')[:500]
    message.save(update_fields=['status', 'failure_code', 'failure_detail', 'updated_at'])
    _close_review_task(message, Task.Status.CANCELLED, 'Cancelled')
    _activity(message, Event.EMAIL_CANCELLED, 'Email cancelled', actor=actor, reason=message.failure_detail)
    return message
