"""Queuing and controlled status changes for email. Nothing here talks to a mail server: delivery
happens only in the `send_due_emails` worker (sending.py).

Workflow services call `queue_email_on_commit(...)` so a message is only created once their
transaction has committed. The application triggers (Slice 2) live in application_emails.py.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
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

from . import stop_conditions
from .models import EmailDraftRevision, EmailMessage, EmailRecipientPreference, EmailSettings, EmailTemplate
from .rendering import VARIABLES, MissingVariableError, TemplateSyntaxError, render
from .windows import next_send_time

logger = logging.getLogger(__name__)
Event = ActivityEvent.EventType
Status = EmailMessage.Status

# Variables only a caller can supply (everything else comes from the vendor, application or weekend).
CALLER_VARIABLES = {'payment_link'}
UNRESOLVED_STATUSES = (Status.DRAFT, Status.QUEUED)


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


def build_context(
    vendor, application=None, market_weekend=None, extra: dict | None = None, application_line=None,
) -> dict[str, str]:
    """The only values a template can see: derived from the records, plus whitelisted caller values.
    A booth line supplies `booth_name` and, unless a weekend is given, its market weekend."""
    owner = vendor.owner
    values = {
        'vendor_name': vendor.brand_name,
        'owner_name': (owner.get_full_name() or owner.username) if owner else 'The Good Flea team',
    }
    if application is not None:
        values['application_reference'] = application_reference(application)
    if application_line is not None:
        values['booth_name'] = application_line.product.name
        market_weekend = market_weekend or application_line.product.weekend
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


def render_template(template: EmailTemplate, values: dict[str, str]) -> tuple[str, str, str]:
    """(subject, html, text). Raises MissingVariableError or TemplateSyntaxError."""
    subject = render(template.subject_template, values, escape_html=False).replace('\n', ' ').strip()
    return (
        subject[:255],
        render(template.html_template, values, escape_html=True),
        render(template.text_template, values, escape_html=False),
    )


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
    actor=None, context: dict | None = None, application_line=None, trigger: dict | None = None,
    force_draft: bool = False,
) -> QueueResult:
    """Renders a template for the vendor contact and records the outcome. Call it through
    `queue_email_on_commit` from workflow services. Idempotent per template + application (or
    vendor) + event_key: a repeat call returns `duplicate` and never overwrites the first message.

    `application_line` and `trigger` identify what the message is about (stop conditions re-check
    them later). `force_draft` (manual preparation) creates a draft whatever the template mode."""
    if not event_key:
        raise ValueError('event_key is required.')
    template = EmailTemplate.objects.filter(key=template_key).first()
    if template is None:
        raise ValueError(f'No email template {template_key!r}.')
    if not template.is_active or (template.mode == EmailTemplate.Mode.OFF and not force_draft):
        return QueueResult('off', reason='The template is off.')

    key = dedupe_key(template_key, vendor=vendor, application=application, event_key=event_key)
    existing = EmailMessage.objects.filter(dedupe_key=key).first()
    if existing:
        return QueueResult('duplicate', existing, 'Already created for this event.')

    try:
        values = build_context(vendor, application, market_weekend, context, application_line=application_line)
        subject, html_body, text_body = render_template(template, values)
    except (MissingVariableError, TemplateSyntaxError) as error:
        logger.warning('Email %s for vendor %s not rendered: %s', template_key, vendor.pk, error)
        record_activity(
            vendor, Event.EMAIL_SKIPPED, f'Email "{template.name}" not created: {error}', actor=actor,
            application=application, template_key=template_key, reason='render_error', event_key=event_key,
        )
        return QueueResult('rejected', reason=str(error))

    if application_line is not None and market_weekend is None:
        market_weekend = application_line.product.weekend
    config = EmailSettings.load()
    now = timezone.now()
    message = EmailMessage(
        template=template, template_key=template.key, vendor=vendor, application=application,
        market_weekend=market_weekend, application_line=application_line, trigger=trigger or {},
        recipient_name=(f'{vendor.first_name} {vendor.last_name}'.strip() or vendor.brand_name),
        recipient_email=(vendor.email or '').strip(),
        sender_name=config.effective_sender_name, sender_email=config.effective_sender_email,
        reply_to_email=config.effective_reply_to,
        subject=subject, html_body=html_body, text_body=text_body,
        dedupe_key=key, event_key=event_key, created_by=actor,
    )
    # Checked when the message is prepared, again before approval, and again before sending.
    stop = stop_conditions.stop_reason(message)
    problem = ('stop_condition', f'{stop[0]}: {stop[1]}') if stop else recipient_problem(vendor.email, template.category)
    if problem:
        message.status, message.scheduled_for = Status.SKIPPED, None
        message.failure_code, message.failure_detail = problem[0], problem[1][:500]
    elif template.mode == EmailTemplate.Mode.DRAFT or force_draft:
        message.status, message.scheduled_for = Status.DRAFT, None
    else:
        message.status = Status.QUEUED
        message.scheduled_for = next_send_time(now, config.business_timezone, config.send_window_start, config.send_window_end)

    try:
        with transaction.atomic():
            message.save()
            if message.status == Status.DRAFT:
                message.review_task = _create_review_task(message, template, actor)
                message.save(update_fields=['review_task', 'updated_at'])
                _activity(message, Event.EMAIL_DRAFTED, f'Email "{template.name}" drafted for review', actor=actor,
                          manual=force_draft)
            elif message.status == Status.QUEUED:
                _activity(message, Event.EMAIL_QUEUED, f'Email "{template.name}" queued', actor=actor,
                          scheduled_for=message.scheduled_for.isoformat())
            else:
                _activity(message, Event.EMAIL_SKIPPED, f'Email "{template.name}" skipped: {problem[1]}', actor=actor,
                          reason=problem[0])
    except IntegrityError:
        # Another process created the same event's message first.
        existing = EmailMessage.objects.filter(dedupe_key=key).first()
        if existing is None:
            raise
        return QueueResult('duplicate', existing, 'Already created for this event.')
    return QueueResult(message.status, message, problem[1] if problem else '')


def _queue_safely(template_key: str, kwargs: dict):
    """Email preparation must never break the workflow that triggered it: failures are logged."""
    try:
        queue_email(template_key, **kwargs)
    except Exception:  # noqa: BLE001
        logger.exception('Could not prepare email %s (event %s)', template_key, kwargs.get('event_key'))


def queue_email_on_commit(template_key: str, **kwargs) -> None:
    """Queues after the surrounding transaction commits (immediately when there is none), so an
    email can never describe a change that was rolled back."""
    transaction.on_commit(lambda: _queue_safely(template_key, kwargs))


def _create_review_task(message: EmailMessage, template: EmailTemplate, actor) -> Task:
    application = message.application
    owner = (application.reviewer if application is not None else None) or message.vendor.owner
    task = Task.objects.create(
        vendor=message.vendor, application=application, task_type=Task.TaskType.EMAIL_REVIEW,
        title=f'Review email: {template.name} to {message.vendor.brand_name}',
        priority=Task.Priority.NORMAL, owner=owner, due_at=discovery_call_due_at(), created_by=actor,
        description=(
            f'Review the drafted "{template.name}" email on the application\'s Emails tab, then approve or cancel it.'
        ),
    )
    record_activity(
        message.vendor, Event.TASK_CREATED, f'Review email task created: {task.title}', actor=actor,
        application=application, task=task, task_type=task.task_type, owner=str(owner.pk) if owner else None,
        due_at=task.due_at.isoformat() if task.due_at else None,
    )
    return task


# --- Preview -------------------------------------------------------------------------------------

SAMPLE_VALUES = {
    'vendor_name': 'Sample Vendor Co.',
    'application_reference': 'APP-SAMPLE01',
    'market_name': 'Oct 10 & 11',
    'market_dates': 'Oct 10 – 11, 2026',
    'booth_name': '8ft x 8ft',
    'owner_name': 'The Good Flea team',
    'payment_link': 'https://example.com/pay/sample',
}
TEMPLATE_PARTS = ('subject_template', 'html_template', 'text_template')


def preview_template(
    template: EmailTemplate, *, application=None, application_line=None, market_weekend=None, overrides: dict | None = None,
) -> dict:
    """Renders a template (or unsaved edits of it) without creating anything. With an application
    the real values are used; otherwise safe sample data. Values the application cannot provide are
    listed in `missing_variables` and shown as [name] in the preview."""
    from .rendering import placeholders, template_errors

    overrides = overrides or {}
    parts = {name: overrides.get(name, getattr(template, name)) for name in TEMPLATE_PARTS}
    allowed = overrides.get('allowed_variables', template.allowed_variables)
    errors = template_errors(parts, allowed)
    if errors:
        raise ValidationError(errors)
    if application is not None:
        values = build_context(application.vendor, application, market_weekend, application_line=application_line)
        source = 'application'
    else:
        values, source = dict(SAMPLE_VALUES), 'sample'
    used = {name for text in parts.values() for name in placeholders(text)}
    missing = sorted(name for name in used if not str(values.get(name, '')).strip())
    filled = {**values, **{name: f'[{name}]' for name in missing}}
    preview = EmailTemplate(**parts, allowed_variables=allowed)
    subject, html_body, text_body = render_template(preview, filled)
    return {'subject': subject, 'html_body': html_body, 'text_body': text_body, 'data_source': source,
            'missing_variables': missing}


# --- Manual drafts -------------------------------------------------------------------------------

def prepare_draft(template_key: str, *, application, actor, application_line=None, market_weekend=None) -> EmailMessage:
    """A team member prepares a draft by hand (any active template, whatever its mode). Refuses
    when it cannot render, the recipient cannot receive it, it no longer applies, or a draft of the
    same template for the same application (and line) is already awaiting review."""
    template = EmailTemplate.objects.filter(key=template_key, is_active=True).first()
    if template is None:
        raise ValidationError({'template_key': 'Choose an active email template.'})
    if application_line is not None and application_line.application_id != application.pk:
        raise ValidationError({'application_line': 'That line belongs to another application.'})
    waiting = EmailMessage.objects.filter(
        template=template, application=application, application_line=application_line, status=Status.DRAFT,
    ).first()
    if waiting:
        raise ValidationError({'template_key': 'A draft of this email is already awaiting review.', 'message_id': waiting.pk})

    from .application_emails import manual_trigger  # the application-specific context

    vendor = application.vendor
    trigger = manual_trigger(template_key, application, application_line)
    try:
        values = build_context(vendor, application, market_weekend, application_line=application_line)
        render_template(template, values)
    except MissingVariableError as error:
        raise ValidationError({'template_key': (
            f'This email needs {", ".join(error.names)}, which this application cannot provide'
            f'{" (choose a booth line)" if {"booth_name", "market_name"} & set(error.names) else ""}.'
        )}) from None
    probe = EmailMessage(template=template, template_key=template.key, vendor=vendor, application=application,
                         application_line=application_line, trigger=trigger)
    stop = stop_conditions.stop_reason(probe)
    if stop:
        raise ValidationError({'template_key': f'This email does not apply right now: {stop[1]}'})
    problem = recipient_problem(vendor.email, template.category)
    if problem:
        raise ValidationError({'recipient': problem[1]})
    result = queue_email(
        template_key, vendor=vendor, application=application, market_weekend=market_weekend,
        application_line=application_line, trigger=trigger, event_key=f'manual:{uuid.uuid4()}', actor=actor,
        force_draft=True,
    )
    return result.message


# --- Permissions ---------------------------------------------------------------------------------

def can_review(message: EmailMessage, user) -> bool:
    """Administrators, the person the review task is assigned to, and the application's reviewer."""
    if is_admin(user):
        return True
    if not (user and user.is_authenticated):
        return False
    task = message.review_task
    if task is not None and task.owner_id == user.pk:
        return True
    return message.application is not None and message.application.reviewer_id == user.pk


def can_edit(message: EmailMessage, user) -> bool:
    """Any active team member may revise a draft; only drafts can be revised."""
    return bool(user and user.is_authenticated and user.is_active) and message.status == Status.DRAFT


# --- Controlled status changes ------------------------------------------------------------------

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
    message = EmailMessage.objects.select_for_update().get(pk=message.pk)
    if not can_review(message, actor):
        raise PermissionDenied('Only administrators, the assigned reviewer or the application reviewer can approve emails.')
    if message.status != Status.DRAFT:
        raise ValidationError({'status': f'Only drafts can be approved (this one is {message.status}).'})
    stop = stop_conditions.stop_reason(message)
    if stop:
        raise ValidationError({'status': f'This email no longer applies: {stop[1].rstrip(".")}. Cancel it instead.'})
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
    message = EmailMessage.objects.select_for_update().get(pk=message.pk)
    if not can_review(message, actor):
        raise PermissionDenied('Only administrators, the assigned reviewer or the application reviewer can cancel emails.')
    return _cancel(message, actor=actor, reason=reason or 'Cancelled by a team member.')


def _cancel(message: EmailMessage, *, actor, reason: str, task_outcome: str = 'Cancelled') -> EmailMessage:
    if message.status not in UNRESOLVED_STATUSES:
        raise ValidationError({'status': f'Only drafts and queued emails can be cancelled (this one is {message.status}).'})
    message.status = Status.CANCELLED
    message.failure_code = 'cancelled'
    message.failure_detail = reason[:500]
    message.save(update_fields=['status', 'failure_code', 'failure_detail', 'updated_at'])
    _close_review_task(message, Task.Status.CANCELLED, task_outcome)
    _activity(message, Event.EMAIL_CANCELLED, 'Email cancelled', actor=actor, reason=message.failure_detail)
    return message


@transaction.atomic
def revise_draft(message: EmailMessage, *, actor, subject: str, html_body: str, text_body: str, note: str = '') -> EmailMessage:
    """Edits an unsent draft. The previous content is kept as an EmailDraftRevision first, so no
    version is ever silently lost. Queued, sent and historical messages cannot be edited."""
    message = EmailMessage.objects.select_for_update().get(pk=message.pk)
    if not can_edit(message, actor):
        raise ValidationError({'status': 'Only drafts awaiting review can be edited.'})
    subject = (subject or '').replace('\n', ' ').strip()
    errors = {}
    if not subject:
        errors['subject'] = 'A subject is required.'
    elif len(subject) > 255:
        errors['subject'] = 'Keep the subject to 255 characters.'
    if not (html_body or '').strip():
        errors['html_body'] = 'The HTML body cannot be empty.'
    if not (text_body or '').strip():
        errors['text_body'] = 'The plain-text body cannot be empty.'
    if errors:
        raise ValidationError(errors)
    if (subject, html_body, text_body) == (message.subject, message.html_body, message.text_body):
        return message
    EmailDraftRevision.objects.create(
        message=message, number=message.revision + 1, subject=message.subject, html_body=message.html_body,
        text_body=message.text_body, replaced_by=actor, note=(note or '')[:300],
    )
    message.subject, message.html_body, message.text_body = subject, html_body, text_body
    message.revision += 1
    message._revising = True
    message.save(update_fields=['subject', 'html_body', 'text_body', 'revision', 'updated_at'])
    _activity(message, Event.EMAIL_REVISED, f'Email draft edited (revision {message.revision})', actor=actor,
              revision=message.revision, note=(note or '')[:300])
    return message


def cancel_stale_drafts() -> int:
    """Cancels drafts whose stop condition now applies (the application was declined, the line is no
    longer waitlisted, ...), closing their review tasks. Run by the worker every minute."""
    cancelled = 0
    for message in EmailMessage.objects.filter(status=Status.DRAFT).select_related('application', 'application_line'):
        stop = stop_conditions.stop_reason(message)
        if not stop:
            continue
        with transaction.atomic():
            locked = EmailMessage.objects.select_for_update().get(pk=message.pk)
            if locked.status != Status.DRAFT:
                continue
            _cancel(locked, actor=None, reason=f'No longer applies: {stop[1]}', task_outcome='No longer applies')
            cancelled += 1
    return cancelled
