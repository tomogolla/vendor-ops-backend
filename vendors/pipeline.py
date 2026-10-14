"""Sales pipeline rules (v1.3): application stages, lines, capacity and discounts.
Views stay thin and call these; every state change writes an ActivityEvent."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts.permissions import is_admin
from catalog.models import AddOn, BoothVariant, MarketWeekend
from catalog.services import confirmed_quantity
from tasks.models import Task
from tasks.services import ensure_application_review

from .activity import record_activity
from .models import ActivityEvent, Application, ApplicationLine, Vendor

Event = ActivityEvent.EventType
Stage = Application.Stage
Status = Application.Status
LineStatus = ApplicationLine.Status

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


@transaction.atomic
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

    cancelled_tasks = []
    if to in CLOSED_STAGES:
        cancelled_tasks = _close_open_work(application, actor, reason)
        close_application_lines(application, actor=actor, reason=f'Application {Stage(to).label.lower()}', note=reason)
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


# --- Lines -----------------------------------------------------------------------

LINE_TRANSITIONS = {
    LineStatus.REQUESTED: {LineStatus.CONFIRMED, LineStatus.WAITLISTED, LineStatus.DECLINED, LineStatus.CANCELLED},
    LineStatus.WAITLISTED: {LineStatus.CONFIRMED, LineStatus.DECLINED, LineStatus.CANCELLED},
    # CONFIRMED -> CANCELLED is "release hold" and needs a reason.
    LineStatus.CONFIRMED: {LineStatus.CANCELLED},
    LineStatus.DECLINED: set(),
    LineStatus.CANCELLED: set(),
}
# Lines can be added or edited while the application is being worked, or once Won (a vendor may
# book more weekends later).
LINE_EDIT_STAGES = {Stage.NEW, Stage.IN_REVIEW, Stage.WON}
# Administrators decide lines on any application that is not declined or withdrawn.
LINE_DECISION_STAGES = {Stage.NEW, Stage.IN_REVIEW, Stage.AWAITING_PAYMENT, Stage.WON}


def _require_admin(actor, action: str):
    if not is_admin(actor):
        raise PermissionDenied(f'Only administrators can {action}.')


def _describe(line: ApplicationLine) -> str:
    product = line.product
    return f'{product.weekend.label} · {product.name} ×{line.quantity}'


def _line_event(line: ApplicationLine, event_type: str, verb: str, actor, **metadata):
    record_activity(
        line.application.vendor, event_type, f'{_describe(line)} {verb}', actor=actor, application=line.application,
        line_id=str(line.pk), product_id=str(line.product.pk), quantity=line.quantity,
        unit_price_cents=line.unit_price_cents, status=line.status, **metadata,
    )


def _check_transition(line: ApplicationLine, to: str):
    if to not in LINE_TRANSITIONS[line.status]:
        raise ValidationError({'status': f'Cannot move a line from {line.status} to {to}.'})


def _lock(line: ApplicationLine) -> ApplicationLine:
    return ApplicationLine.objects.select_for_update(of=('self',)).select_related(
        'application__vendor', 'variant__weekend', 'add_on__weekend', 'parent_line',
    ).get(pk=line.pk)


@transaction.atomic
def add_line(
    application: Application, *, actor, variant: BoothVariant | None = None, add_on: AddOn | None = None,
    parent_line: ApplicationLine | None = None, quantity: int = 1,
) -> ApplicationLine:
    """Any authenticated user may request lines. The unit price is copied from the product now
    and never recalculated."""
    application = Application.objects.select_for_update(of=('self',)).select_related('vendor').get(pk=application.pk)
    if application.stage not in LINE_EDIT_STAGES:
        raise ValidationError({'application': 'Lines can be added only while the application is New, In review or Won.'})
    if (variant is None) == (add_on is None):
        raise ValidationError({'variant': 'Choose exactly one of a booth variant or an add-on.'})
    if quantity < 1:
        raise ValidationError({'quantity': 'Quantity must be at least 1.'})
    product = variant or add_on
    if product.weekend.status == MarketWeekend.Status.CLOSED:
        raise ValidationError({'weekend': f'{product.weekend.label} is closed.'})

    open_lines = application.lines.filter(status__in=ApplicationLine.OPEN_STATUSES)
    if add_on is not None:
        if parent_line is None:
            raise ValidationError({'parent_line': 'An add-on line must belong to a booth line.'})
        if (parent_line.application_id != application.pk or parent_line.variant_id is None
                or parent_line.variant.weekend_id != add_on.weekend_id):
            raise ValidationError({'parent_line': 'The parent must be a booth line for the same weekend on this application.'})
        if parent_line.status not in ApplicationLine.OPEN_STATUSES:
            raise ValidationError({'parent_line': 'That booth line is closed.'})
        duplicate = open_lines.filter(add_on=add_on, parent_line=parent_line).first()
    else:
        if parent_line is not None:
            raise ValidationError({'parent_line': 'Only add-on lines have a parent line.'})
        duplicate = open_lines.filter(variant=variant).first()
    if duplicate:
        # Duplicates are rejected rather than merged: change the quantity on the open line instead.
        raise ValidationError({
            'detail': f'This application already has an open line for {product.name} on {product.weekend.label}; '
                      'change its quantity instead.',
            'line_id': str(duplicate.pk),
        })

    line = ApplicationLine.objects.create(
        application=application, variant=variant, add_on=add_on, parent_line=parent_line,
        quantity=quantity, unit_price_cents=product.price_cents, created_by=actor,
    )
    _line_event(line, Event.LINE_ADDED, 'requested', actor)
    return line


@transaction.atomic
def update_line(line: ApplicationLine, *, actor, quantity: int) -> ApplicationLine:
    line = _lock(line)
    if line.status != LineStatus.REQUESTED:
        raise ValidationError({'status': 'Only requested lines can be changed.'})
    if line.application.stage not in LINE_EDIT_STAGES:
        raise ValidationError({'application': 'Lines can be changed only while the application is New, In review or Won.'})
    if quantity < 1:
        raise ValidationError({'quantity': 'Quantity must be at least 1.'})
    before = line.quantity
    if quantity != before:
        line.quantity = quantity
        line.save(update_fields=['quantity', 'updated_at'])
        _line_event(line, Event.LINE_UPDATED, 'quantity changed', actor, **{'from': before, 'to': quantity})
    return line


@transaction.atomic
def remove_line(line: ApplicationLine, *, actor):
    line = _lock(line)
    if line.status != LineStatus.REQUESTED:
        raise ValidationError({'status': 'Only requested lines can be removed; decided lines are declined or released.'})
    if line.application.stage not in LINE_EDIT_STAGES:
        raise ValidationError({'application': 'Lines can be removed only while the application is New, In review or Won.'})
    children = list(line.add_on_lines.all())
    if any(child.status != LineStatus.REQUESTED for child in children):
        raise ValidationError({'detail': 'This booth line has add-on lines that were already decided.'})
    for target in [*children, line]:
        _line_event(target, Event.LINE_REMOVED, 'removed', actor)
        target.delete()


def _close_line(line: ApplicationLine, to: str, *, actor, reason: str, event_type: str, verb: str, **metadata):
    line.status = to
    line.status_reason = reason
    line.closed_at = timezone.now()
    line.save(update_fields=['status', 'status_reason', 'closed_at', 'updated_at'])
    _line_event(line, event_type, verb, actor, reason=reason, **metadata)


def _cascade_to_add_ons(line: ApplicationLine, *, actor, reason: str):
    """Closing a booth line closes its open add-on lines."""
    children = ApplicationLine.objects.select_for_update().filter(
        parent_line=line, status__in=ApplicationLine.OPEN_STATUSES,
    )
    for child in children:
        released = child.status == LineStatus.CONFIRMED
        _close_line(
            child, LineStatus.CANCELLED, actor=actor, reason=reason,
            event_type=Event.LINE_RELEASED if released else Event.LINE_CANCELLED,
            verb='hold released' if released else 'cancelled', parent_line_id=str(line.pk),
        )


@transaction.atomic
def confirm_line(line: ApplicationLine, *, actor) -> ApplicationLine:
    _require_admin(actor, 'confirm lines')
    line = _lock(line)
    _check_transition(line, LineStatus.CONFIRMED)
    if line.application.stage not in LINE_DECISION_STAGES:
        raise ValidationError({'application': 'Lines on a declined or withdrawn application cannot be confirmed.'})
    if line.add_on_id and line.parent_line.status != LineStatus.CONFIRMED:
        raise ValidationError({'parent_line': 'Confirm the booth line before its add-ons.'})

    # Lock the product row so two administrators cannot confirm the last spots at the same time.
    product = type(line.product).objects.select_for_update().get(pk=line.product.pk)
    if product.capacity is not None:
        remaining = product.capacity - confirmed_quantity(product)
        if line.quantity > remaining:
            raise ValidationError({
                'quantity': f'Only {max(remaining, 0)} of {product.name} left for {product.weekend.label}; '
                            f'this line needs {line.quantity}. Waitlist it instead.',
                'remaining': max(remaining, 0),
            })

    line.status = LineStatus.CONFIRMED
    line.confirmed_at = timezone.now()
    line.status_reason = ''
    line.save(update_fields=['status', 'confirmed_at', 'status_reason', 'updated_at'])
    _line_event(line, Event.LINE_CONFIRMED, 'confirmed', actor)
    return line


@transaction.atomic
def waitlist_line(line: ApplicationLine, *, actor, reason='') -> ApplicationLine:
    _require_admin(actor, 'waitlist lines')
    line = _lock(line)
    _check_transition(line, LineStatus.WAITLISTED)
    if line.application.stage not in LINE_DECISION_STAGES:
        raise ValidationError({'application': 'Lines on a declined or withdrawn application cannot be waitlisted.'})
    line.status = LineStatus.WAITLISTED
    line.waitlisted_at = timezone.now()
    line.status_reason = reason
    line.save(update_fields=['status', 'waitlisted_at', 'status_reason', 'updated_at'])
    _line_event(line, Event.LINE_WAITLISTED, 'waitlisted', actor, reason=reason)
    return line


@transaction.atomic
def decline_line(line: ApplicationLine, *, actor, reason='') -> ApplicationLine:
    _require_admin(actor, 'decline lines')
    line = _lock(line)
    _check_transition(line, LineStatus.DECLINED)
    _close_line(line, LineStatus.DECLINED, actor=actor, reason=reason, event_type=Event.LINE_DECLINED, verb='declined')
    _cascade_to_add_ons(line, actor=actor, reason=reason or 'Booth line declined')
    return line


@transaction.atomic
def release_line(line: ApplicationLine, *, actor, reason: str) -> ApplicationLine:
    """Release hold: a confirmed line gives its capacity back."""
    _require_admin(actor, 'release holds')
    line = _lock(line)
    if line.status != LineStatus.CONFIRMED:
        raise ValidationError({'status': 'Only confirmed lines hold capacity to release.'})
    if not (reason or '').strip():
        raise ValidationError({'reason': 'Say why the hold is released.'})
    if line.application.first_paid_at:
        raise ValidationError('Refund rules are not implemented yet')
    _close_line(
        line, LineStatus.CANCELLED, actor=actor, reason=reason, event_type=Event.LINE_RELEASED, verb='hold released',
    )
    _cascade_to_add_ons(line, actor=actor, reason=reason)
    return line


def close_application_lines(application: Application, *, actor, reason: str, note=''):
    """Declined or withdrawn: cancel requested and waitlisted lines and release confirmed ones."""
    full_reason = f'{reason}: {note}' if note else reason
    # Query ApplicationLine directly: a prefetched `application.lines` carries outer joins that
    # FOR UPDATE cannot lock.
    open_booths = ApplicationLine.objects.select_for_update().filter(
        application=application, status__in=ApplicationLine.OPEN_STATUSES, parent_line__isnull=True,
    )
    for line in open_booths:
        released = line.status == LineStatus.CONFIRMED
        if released and application.first_paid_at:
            raise ValidationError('Refund rules are not implemented yet')
        _close_line(
            line, LineStatus.CANCELLED, actor=actor, reason=full_reason,
            event_type=Event.LINE_RELEASED if released else Event.LINE_CANCELLED,
            verb='hold released' if released else 'cancelled',
        )
        _cascade_to_add_ons(line, actor=actor, reason=full_reason)


# --- Totals --------------------------------------------------------------------------

def discount_cents(discount_type: str, value, subtotal_cents: int) -> int:
    """PERCENT of the subtotal rounded half-up to the cent, or FIXED cents; never more than the subtotal."""
    if discount_type == Application.DiscountType.PERCENT:
        cents = (Decimal(subtotal_cents) * Decimal(value) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    elif discount_type == Application.DiscountType.FIXED:
        cents = Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    else:
        return 0
    return max(0, min(int(cents), subtotal_cents))


def application_totals(application: Application, lines=None) -> dict:
    lines = list(application.lines.all()) if lines is None else lines
    requested = sum(line.total_cents for line in lines if line.status in ApplicationLine.OPEN_STATUSES)
    subtotal = sum(line.total_cents for line in lines if line.status == LineStatus.CONFIRMED)
    discount = discount_cents(application.discount_type, application.discount_value, subtotal)
    return {
        'currency': settings.CURRENCY,
        'requested_total_cents': requested,
        'confirmed_subtotal_cents': subtotal,
        'discount_cents': discount,
        'confirmed_total_cents': subtotal - discount,
    }
