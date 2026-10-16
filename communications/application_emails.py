"""Slice 2: the four application emails, and the stop conditions that keep them accurate.

Each trigger prepares its email through `queue_email_on_commit`, so nothing is created unless
the workflow change commits, and the template mode decides what happens (all four are Off by
default: nothing is created until an administrator switches a template to Draft or Auto).

| Template               | Trigger                                              | One per                       |
| ---------------------- | ---------------------------------------------------- | ----------------------------- |
| application_received   | a genuinely new application (not a bundled one)      | application                   |
| call_follow_up         | a call attempt logged as "No Answer"                 | call task (the first one)     |
| waitlist_update        | a booth line moved to WAITLISTED                     | booth line                    |
| application_declined   | the application moved to DECLINED                    | decision (each decline event) |

Stop conditions run when a message is prepared, before a draft is approved, every minute for
drafts still awaiting review (stale ones are cancelled), and immediately before sending.
"""

from datetime import datetime

from tasks.models import Task
from vendors.models import Application, ApplicationLine

from . import stop_conditions
from .services import queue_email_on_commit

Stage = Application.Stage
CallOutcome = Task.CallOutcome
CALL_TYPES = (Task.TaskType.DISCOVERY_CALL, Task.TaskType.FOLLOW_UP)
# Outcomes where somebody actually spoke to the vendor (same meaning as the board's call filter).
REACHED_OUTCOMES = (CallOutcome.INTERESTED, CallOutcome.NOT_QUALIFIED, CallOutcome.CALLBACK_REQUESTED)
CLOSED_STAGES = (Stage.DECLINED, Stage.WITHDRAWN)


# --- Triggers ---------------------------------------------------------------------------------

def application_submitted(application, actor):
    """A new application (submit_application only calls this when nothing was bundled)."""
    queue_email_on_commit(
        'application_received', vendor=application.vendor, application=application, actor=actor,
        event_key=f'application:{application.pk}:received',
    )


def call_logged(task, actor):
    """Only "No Answer" asks for an email ("we tried to reach you"); when the vendor answered,
    or the number was wrong, nobody should guess: the team can prepare a draft by hand. One
    automatic follow-up per call task, however many attempts it takes."""
    if task.task_type not in CALL_TYPES or task.outcome != CallOutcome.NO_ANSWER:
        return
    queue_email_on_commit(
        'call_follow_up', vendor=task.vendor, application=task.application, actor=actor,
        event_key=f'call-task:{task.pk}:no-answer',
        trigger={
            'call_task_id': str(task.pk), 'attempt': task.attempt_count,
            'attempted_at': task.last_attempt_at.isoformat() if task.last_attempt_at else None,
        },
    )


def line_waitlisted(line, actor):
    """A booth line went to the waitlist. Add-on waitlists get no automatic email (the copy is about
    booths); the team can prepare one by hand."""
    if line.variant_id is None:
        return
    queue_email_on_commit(
        'waitlist_update', vendor=line.application.vendor, application=line.application, application_line=line,
        actor=actor, event_key=f'line:{line.pk}:waitlisted',
        trigger={'line_id': str(line.pk), 'waitlisted_at': line.waitlisted_at.isoformat() if line.waitlisted_at else None},
    )


def application_declined(application, actor):
    """Each decline is its own decision event: declining again after a reopen prepares a new email."""
    decided_at = application.decided_at.isoformat() if application.decided_at else ''
    queue_email_on_commit(
        'application_declined', vendor=application.vendor, application=application, actor=actor,
        event_key=f'application:{application.pk}:declined:{decided_at}', trigger={'decided_at': decided_at},
    )


def manual_trigger(template_key: str, application, application_line=None) -> dict:
    """What a hand-prepared draft is about, so the same stop conditions protect it."""
    if template_key == 'application_declined' and application.decided_at:
        return {'decided_at': application.decided_at.isoformat(), 'manual': True}
    if template_key == 'waitlist_update' and application_line is not None:
        return {'line_id': str(application_line.pk), 'manual': True}
    return {'manual': True}


# --- Stop conditions -------------------------------------------------------------------------------

def _closed(message) -> str | None:
    if message.application_id is None:
        return None
    stage = Application.objects.filter(pk=message.application_id).values_list('stage', flat=True).first()
    if stage in CLOSED_STAGES:
        return f'The application is {Stage(stage).label.lower()}.'
    return None


@stop_conditions.register('application_received')
def received_application_still_open(message):
    return _closed(message)


@stop_conditions.register('call_follow_up')
def follow_up_still_needed(message):
    closed = _closed(message)
    if closed:
        return closed
    attempted_at = (message.trigger or {}).get('attempted_at')
    if attempted_at and message.vendor_id:
        reached = Task.objects.filter(
            vendor_id=message.vendor_id, task_type__in=CALL_TYPES, outcome__in=REACHED_OUTCOMES,
            last_attempt_at__gt=datetime.fromisoformat(attempted_at),
        ).exists()
        if reached:
            return 'The vendor has been reached since this call attempt.'
    return None


@stop_conditions.register('waitlist_update')
def line_still_waitlisted(message):
    closed = _closed(message)
    if closed:
        return closed
    line = message.application_line
    if line is None:
        return 'No booth line is linked to this waitlist email.'
    line = ApplicationLine.objects.filter(pk=line.pk).first() or line  # current status, not a cached one
    if line.status != ApplicationLine.Status.WAITLISTED:
        return f'The {line.product.name} line for {line.product.weekend.label} is no longer waitlisted ({line.get_status_display().lower()}).'
    return None


@stop_conditions.register('application_declined')
def still_declined(message):
    application = message.application
    if application is None:
        return 'No application is linked to this decline email.'
    application = Application.objects.filter(pk=application.pk).first() or application
    if application.stage != Stage.DECLINED:
        return f'The application is no longer declined (it is {application.get_stage_display().lower()}).'
    decided_at = (message.trigger or {}).get('decided_at')
    if decided_at and application.decided_at and application.decided_at.isoformat() != decided_at:
        return 'The application was reopened and declined again; this email is for the earlier decision.'
    return None
