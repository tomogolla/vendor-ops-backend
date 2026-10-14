"""Sales pipeline rules (v1.3): application stages and discounts.
Views stay thin and call these; every state change writes an ActivityEvent."""

from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts.permissions import is_admin
from tasks.models import Task
from tasks.services import ensure_application_review

from .activity import record_activity
from .models import ActivityEvent, Application, Vendor

Event = ActivityEvent.EventType
Stage = Application.Stage
Status = Application.Status

# Stages an administrator may move an application between by hand.
MANUAL_STAGE_TRANSITIONS = {
    Stage.NEW: {Stage.IN_REVIEW, Stage.DECLINED, Stage.WITHDRAWN},
    Stage.IN_REVIEW: {Stage.DECLINED, Stage.WITHDRAWN},
    # TODO(decision): declined cards. DECLINED and WITHDRAWN are provisional terminal stages;
    # until that decision is made either can be reopened to IN_REVIEW, as before v1.3.
    Stage.DECLINED: {Stage.IN_REVIEW},
    Stage.WITHDRAWN: {Stage.IN_REVIEW},
    Stage.AWAITING_PAYMENT: set(),
    Stage.WON: set(),
}
# Reached only through mark_billed / mark_first_payment, and never left by hand.
BILLING_STAGES = {Stage.AWAITING_PAYMENT, Stage.WON}
CLOSED_STAGES = {Stage.DECLINED, Stage.WITHDRAWN}
CLOSABLE_TASK_TYPES = (Task.TaskType.DISCOVERY_CALL, Task.TaskType.APPLICATION_REVIEW)


def sync_application_summary(vendor: Vendor):
    """Keeps the deprecated vendor fields in step with the applications."""
    latest = vendor.applications.order_by('-submitted_at').first()
    vendor.application_status_summary = latest.derived_status if latest else ''
    if latest and latest.stage in BILLING_STAGES and vendor.journey_stage != Vendor.JourneyStage.SUSPENDED:
        vendor.journey_stage = Vendor.JourneyStage.APPROVED
    vendor.save(update_fields=['application_status_summary', 'journey_stage'])


# --- Stage changes -------------------------------------------------------------

def change_application_stage(application: Application, to: str, actor, reason='') -> Application:
    """Manual stage change by an administrator."""
    if not is_admin(actor):
        raise PermissionDenied('Only administrators can change the application stage.')
    if to not in Stage.values:
        raise ValidationError({'stage': f'Choose one of: {", ".join(Stage.values)}.'})
    current = application.stage
    if current in BILLING_STAGES:
        raise ValidationError({'stage': (
            f'An application that is {Stage(current).label} cannot be moved by hand: '
            'cancellation and refunds are not designed yet.'
        )})
    if to in BILLING_STAGES:
        raise ValidationError({'stage': (
            f'{Stage(to).label} is reached only by marking the application billed or paid.'
        )})
    if to not in MANUAL_STAGE_TRANSITIONS.get(current, set()):
        raise ValidationError({'stage': f'Cannot move an application from {current} to {to}.'})
    return move_stage(application, to, actor=actor, reason=reason)


def move_stage(application: Application, to: str, *, actor, reason='', system=False, **metadata) -> Application:
    """Applies a stage change that the caller has already validated. `system` marks automatic
    moves (for example a logged call moving NEW to IN_REVIEW) made on behalf of `actor`."""
    current = application.stage
    now = timezone.now()
    application.stage = to
    application.stage_entered_at = now
    application.status = Application.STAGE_TO_STATUS[to]
    if to == Stage.DECLINED or to == Stage.AWAITING_PAYMENT:
        application.decision = application.status
        application.decision_reason = reason
        application.decided_at = now
        if application.reviewer_id is None and actor is not None and not system:
            application.reviewer = actor
    application.save()

    cancelled_tasks = _close_open_work(application, actor, reason) if to in CLOSED_STAGES else []
    if to == Stage.IN_REVIEW:
        ensure_application_review(application, actor=actor)

    sync_application_summary(application.vendor)
    record_activity(
        application.vendor, Event.APPLICATION_STAGE_CHANGED,
        f'Application {Stage(current).label} → {Stage(to).label}', actor=actor, application=application,
        **{'from': current, 'to': to, 'reason': reason, 'system': system,
           'cancelled_task_ids': cancelled_tasks, **metadata},
    )
    return application


def _close_open_work(application: Application, actor, reason: str) -> list[str]:
    """Declined or withdrawn: cancel the open call and review tasks."""
    open_tasks = Task.objects.filter(
        application=application, task_type__in=CLOSABLE_TASK_TYPES, status__in=Task.OPEN_STATUSES,
    )
    # A discovery call created before the application (pre-v1.3 leads) is not linked to it; cancel
    # it too unless the vendor has another application still being worked.
    vendor = application.vendor
    other_open = vendor.applications.exclude(pk=application.pk).exclude(stage__in=CLOSED_STAGES).exists()
    unlinked_call = Task.objects.none() if other_open else Task.objects.filter(
        vendor=vendor, application__isnull=True, task_type=Task.TaskType.DISCOVERY_CALL,
        status__in=Task.OPEN_STATUSES,
    )
    cancelled = []
    for task in [*open_tasks, *unlinked_call]:
        task.status = Task.Status.CANCELLED
        task.save(update_fields=['status', 'updated_at'])
        cancelled.append(str(task.pk))
    return cancelled


# --- Discounts -----------------------------------------------------------------

def set_discount(application: Application, *, actor, discount_type: str, value, label='') -> Application:
    if not is_admin(actor):
        raise PermissionDenied('Only administrators can set discounts.')
    if discount_type not in Application.DiscountType.values:
        raise ValidationError({'discount_type': f'Choose one of: {", ".join(Application.DiscountType.values)}.'})
    try:
        amount = Decimal(str(value if value not in (None, '') else 0))
    except InvalidOperation as error:
        raise ValidationError({'discount_value': 'Enter a number.'}) from error
    if discount_type == Application.DiscountType.PERCENT and not Decimal(0) <= amount <= Decimal(100):
        raise ValidationError({'discount_value': 'A percent discount must be between 0 and 100.'})
    if discount_type == Application.DiscountType.FIXED and (amount < 0 or amount != amount.to_integral_value()):
        raise ValidationError({'discount_value': 'A fixed discount is a whole number of cents, 0 or more.'})
    if discount_type == Application.DiscountType.NONE:
        amount, label = Decimal(0), ''
    if amount.as_tuple().exponent < -2:
        raise ValidationError({'discount_value': 'Use at most two decimal places.'})

    before = {
        'type': application.discount_type, 'value': str(application.discount_value), 'label': application.discount_label,
    }
    application.discount_type = discount_type
    application.discount_value = amount
    application.discount_label = label
    application.save(update_fields=['discount_type', 'discount_value', 'discount_label', 'updated_at'])
    record_activity(
        application.vendor, Event.APPLICATION_DISCOUNT_CHANGED,
        f'Discount set: {application.get_discount_type_display()}'
        + (f' {amount}' if discount_type != Application.DiscountType.NONE else ''),
        actor=actor, application=application,
        **{'from': before, 'to': {'type': discount_type, 'value': str(amount), 'label': label}},
    )
    return application
