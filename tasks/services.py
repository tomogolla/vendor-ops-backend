from datetime import datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from vendors.activity import record_activity
from vendors.models import ActivityEvent, Application, Vendor

from .models import Task
from .scheduling import discovery_call_due_at

Event = ActivityEvent.EventType


def _create_unique_open_task(lookup: dict, defaults: dict, actor) -> tuple[Task, bool]:
    """Creates a task unless an open one matches `lookup`; the partial unique constraints
    make this safe under concurrent requests."""
    open_tasks = Task.objects.filter(status__in=Task.OPEN_STATUSES, **lookup)
    existing = open_tasks.first()
    if existing:
        return existing, False
    try:
        with transaction.atomic():
            task = Task.objects.create(**lookup, **defaults, created_by=actor)
    except IntegrityError:
        return open_tasks.get(), False
    record_activity(
        task.vendor, Event.TASK_CREATED, f'{task.get_task_type_display()} task created: {task.title}',
        actor=actor, application=task.application, task=task,
        task_type=task.task_type, owner=str(task.owner_id) if task.owner_id else None,
        due_at=task.due_at.isoformat() if task.due_at else None,
    )
    return task, True


def already_reached(vendor: Vendor) -> bool:
    """A first-contact call already succeeded, so another discovery call is not needed."""
    return Task.objects.filter(
        vendor=vendor, task_type=Task.TaskType.DISCOVERY_CALL,
        status=Task.Status.COMPLETED, outcome=Task.CallOutcome.INTERESTED,
    ).exists()


def ensure_discovery_call(
    vendor: Vendor, actor=None, owner=None, application: Application | None = None, skip_if_reached=False,
):
    """One open discovery call per vendor. Returns (task | None, created)."""
    if not vendor.is_active or vendor.outreach_opt_out:
        return None, False
    if skip_if_reached and already_reached(vendor):
        return None, False
    return _create_unique_open_task(
        {'vendor': vendor, 'task_type': Task.TaskType.DISCOVERY_CALL},
        {
            'application': application,
            'title': f'Discovery call: {vendor.brand_name}',
            'priority': Task.Priority.HIGH,
            'owner': owner if owner is not None else vendor.owner,
            'due_at': discovery_call_due_at(),
            'description': 'First-contact call: introduce The Good Flea, qualify the brand and capture the call script answers.',
        },
        actor,
    )


def ensure_application_review(application: Application, actor=None, reviewer=None):
    return _create_unique_open_task(
        {'vendor': application.vendor, 'application': application, 'task_type': Task.TaskType.APPLICATION_REVIEW},
        {
            'title': f'Review application: {application.vendor.brand_name}',
            'priority': Task.Priority.NORMAL,
            'owner': reviewer or application.reviewer,
            'due_at': discovery_call_due_at(),
            'description': 'Review the application and record a decision.',
        },
        actor,
    )


CLOSING_OUTCOMES = {
    Task.CallOutcome.INTERESTED,
    Task.CallOutcome.NOT_QUALIFIED,
    Task.CallOutcome.WRONG_NUMBER,
}


def log_call(
    task: Task,
    actor,
    outcome: str,
    notes: str = '',
    call_details: dict | None = None,
    callback_at: datetime | None = None,
) -> Task:
    if task.task_type not in (Task.TaskType.DISCOVERY_CALL, Task.TaskType.FOLLOW_UP):
        raise ValidationError({'task': 'Calls can only be logged on discovery call or follow-up tasks.'})
    if not task.is_open:
        raise ValidationError({'task': 'This task is already closed.'})
    if outcome not in Task.CallOutcome.values:
        raise ValidationError({'outcome': f'Choose one of: {", ".join(Task.CallOutcome.values)}.'})
    if outcome == Task.CallOutcome.CALLBACK_REQUESTED and not callback_at:
        raise ValidationError({'callback_at': 'Set a callback time for a callback request.'})

    now = timezone.now()
    task.attempt_count += 1
    task.last_attempt_at = now
    task.outcome = outcome
    if notes:
        task.notes = f'{task.notes}\n\n{notes}'.strip() if task.notes else notes
    if call_details is not None:
        task.call_details = call_details
    if task.owner_id is None:
        task.owner = actor

    out_of_attempts = task.attempt_count >= settings.MAX_CALL_ATTEMPTS
    if outcome in CLOSING_OUTCOMES or (outcome == Task.CallOutcome.NO_ANSWER and out_of_attempts):
        task.status = Task.Status.COMPLETED
        task.completed_at = now
    elif outcome == Task.CallOutcome.CALLBACK_REQUESTED:
        task.status = Task.Status.RESCHEDULED
        task.callback_at = callback_at
        task.due_at = callback_at
    else:
        task.status = Task.Status.NO_ANSWER
    task.save()

    event = Event.CALL_COMPLETED if task.status == Task.Status.COMPLETED else Event.CALL_ATTEMPTED
    record_activity(
        task.vendor, event, f'Call attempt {task.attempt_count}: {outcome}',
        actor=actor, application=task.application, task=task,
        outcome=outcome, attempt=task.attempt_count, notes=notes,
        call_details=task.call_details,
        callback_at=callback_at.isoformat() if callback_at else None,
    )
    if task.task_type == Task.TaskType.DISCOVERY_CALL and task.application_id:
        from vendors.pipeline import advance_after_call_attempt  # vendors.pipeline imports this module

        advance_after_call_attempt(task.application, actor)
    from communications.application_emails import call_logged  # communications imports tasks

    call_logged(task, actor)
    return task
