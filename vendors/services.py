"""Business rules for vendors and applications. Views stay thin and call these."""

from dataclasses import dataclass, field

from django.db.models import Q
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts.permissions import is_admin
from tasks.services import ensure_application_review, ensure_discovery_call

from .activity import record_activity
from .models import ActivityEvent, Application, InternalNote, LeadIntakeEvent, Vendor
from .normalization import normalize_email, normalize_instagram, normalize_phone
from .pipeline import change_application_stage, sync_application_summary

Event = ActivityEvent.EventType
Stage = Application.Stage


# --- Duplicate matching ------------------------------------------------------

@dataclass
class Match:
    vendor: Vendor
    matched_on: list[str] = field(default_factory=list)


@dataclass
class MatchResult:
    reliable: list[Match]
    name_only: list[Vendor]

    @property
    def single(self) -> Vendor | None:
        return self.reliable[0].vendor if len(self.reliable) == 1 else None


def find_matches(*, email='', phone='', instagram='', brand_name='', exclude_id=None) -> MatchResult:
    """Reliable matches are exact normalized email, phone or Instagram. A business name
    on its own is only ever a hint and is never used to merge."""
    keys = {
        'email': ('email_normalized', normalize_email(email)),
        'phone': ('phone_normalized', normalize_phone(phone)),
        'instagram': ('instagram_normalized', normalize_instagram(instagram)),
    }
    query = Q()
    for column, value in keys.values():
        if value:
            query |= Q(**{column: value})

    reliable: list[Match] = []
    if query:
        candidates = Vendor.objects.filter(query)
        if exclude_id:
            candidates = candidates.exclude(pk=exclude_id)
        for vendor in candidates:
            matched_on = [name for name, (column, value) in keys.items() if value and getattr(vendor, column) == value]
            reliable.append(Match(vendor, matched_on))

    name_only: list[Vendor] = []
    if brand_name.strip():
        found = {m.vendor.pk for m in reliable}
        same_name = Vendor.objects.filter(brand_name__iexact=brand_name.strip()).exclude(pk__in=found)
        if exclude_id:
            same_name = same_name.exclude(pk=exclude_id)
        name_only = list(same_name[:10])
    return MatchResult(reliable, name_only)


class DuplicateVendor(Exception):
    def __init__(self, result: MatchResult):
        self.result = result


# --- Activity-producing helpers ----------------------------------------------

def _check_owner_assignment(actor, owner):
    if owner is not None and owner != actor and not is_admin(actor):
        raise PermissionDenied('Only administrators can assign vendors or tasks to other people.')


# --- Lead registration ---------------------------------------------------------

def register_lead(*, actor, data: dict, owner=None, confirm_duplicate=False, source_detail='') -> Vendor:
    _check_owner_assignment(actor, owner)
    result = find_matches(
        email=data.get('email', ''), phone=data.get('phone', ''), instagram=data.get('instagram', ''),
    )
    if result.reliable and not confirm_duplicate:
        raise DuplicateVendor(result)

    vendor = Vendor.objects.create(**data, owner=owner, created_by=actor)
    LeadIntakeEvent.objects.create(
        vendor=vendor,
        source_type=LeadIntakeEvent.SourceType.MANUAL_REGISTRATION,
        source_detail=source_detail or vendor.lead_source,
        captured_by=actor,
        metadata={'confirmed_possible_duplicate_of': [str(m.vendor.pk) for m in result.reliable]},
    )
    record_activity(
        vendor, Event.LEAD_REGISTERED, f'{vendor.brand_name} registered as a lead', actor=actor,
        lead_source=vendor.lead_source,
    )
    ensure_discovery_call(vendor, actor=actor, owner=owner)
    return vendor


# --- Application submission --------------------------------------------------

class AmbiguousVendorMatch(Exception):
    def __init__(self, result: MatchResult):
        self.result = result


def submit_application(
    *, actor, vendor: Vendor | None = None, vendor_data: dict | None = None,
    application_data: dict | None = None, source='Staff entry',
) -> Application:
    vendor_data = vendor_data or {}
    application_data = application_data or {}

    created_vendor = False
    if vendor is None:
        result = find_matches(
            email=vendor_data.get('email', ''), phone=vendor_data.get('phone', ''),
            instagram=vendor_data.get('instagram', ''),
        )
        if len(result.reliable) > 1:
            raise AmbiguousVendorMatch(result)
        vendor = result.single
        if vendor is None:
            vendor = Vendor.objects.create(**vendor_data, created_by=actor)
            created_vendor = True

    LeadIntakeEvent.objects.create(
        vendor=vendor,
        source_type=LeadIntakeEvent.SourceType.APPLICATION_SUBMISSION,
        source_detail=source,
        captured_by=actor,
        metadata={'created_vendor': created_vendor},
    )
    if vendor.journey_stage == Vendor.JourneyStage.LEAD:
        vendor.journey_stage = Vendor.JourneyStage.APPLICANT
        vendor.save(update_fields=['journey_stage'])

    application = Application.objects.create(
        vendor=vendor,
        submitted_by=actor,
        source=source,
        category=application_data.get('category') or vendor.category,
        product_description=application_data.get('product_description') or vendor.description,
        social_snapshot={'website': vendor.website, 'instagram': vendor.instagram},
        **{k: v for k, v in application_data.items() if k not in {'category', 'product_description'}},
    )
    sync_application_summary(vendor)
    record_activity(
        vendor, Event.APPLICATION_SUBMITTED, f'Application submitted ({source})', actor=actor,
        application=application, created_vendor=created_vendor,
    )
    ensure_discovery_call(vendor, actor=actor, application=application, skip_if_reached=True)
    ensure_application_review(application, actor=actor)
    return application


# --- Qualification and the deprecated vendor status --------------------------

def set_qualification(vendor: Vendor, to: str, *, actor, reason='') -> Vendor:
    if not is_admin(actor):
        raise PermissionDenied('Only administrators can change qualification.')
    if to not in Vendor.Qualification.values:
        raise ValidationError({'qualification_status': 'Unknown qualification status.'})
    current = vendor.qualification_status
    if current == to:
        return vendor
    vendor.qualification_status = to
    vendor.save(update_fields=['qualification_status'])
    record_activity(
        vendor, Event.QUALIFICATION_CHANGED,
        f'Qualification {Vendor.Qualification(current).label} → {Vendor.Qualification(to).label}',
        actor=actor, **{'from': current, 'to': to, 'reason': reason},
    )
    return vendor


# DEPRECATED (v1.3): the current frontend's single-status model behind POST /vendors/{id}/status/.
# It is translated onto application stages here and goes away with the frontend update.
DISPLAY_TRANSITIONS = {
    'lead': {'applied', 'in_review', 'declined'},
    'applied': {'in_review', 'declined'},
    'in_review': {'declined'},
    'approved': {'suspended'},
    'suspended': {'approved'},
    'declined': {'in_review'},
}
DISPLAY_TO_STAGE = {'in_review': Stage.IN_REVIEW, 'declined': Stage.DECLINED}
LINE_DECISIONS_MESSAGE = (
    'Approving and waitlisting are now decided per booth line. Use POST /api/v1/application-lines/{id}/confirm/ '
    'or /waitlist/, then POST /api/v1/applications/{id}/mark-billed/.'
)


class UseLineDecisions(ValidationError):
    def __init__(self):
        super().__init__({'status': LINE_DECISIONS_MESSAGE, 'code': 'use_line_decisions'})


def change_vendor_status(vendor: Vendor, to: str, *, actor, note='') -> Vendor:
    """DEPRECATED (v1.3): kept for the live frontend. Supports applied -> in_review, decline,
    reopen, suspend and reinstate; 'approved' (other than reinstating) and 'waitlisted' are refused."""
    current = vendor.display_status
    if to == 'waitlisted' or (to == 'approved' and current != 'suspended'):
        raise UseLineDecisions()
    if to not in DISPLAY_TRANSITIONS.get(current, set()):
        raise ValidationError({'status': f'Cannot move from {current} to {to}.'})
    # Moving a lead to "applied" is an application submission, which team members may do.
    if not (current == 'lead' and to == 'applied') and not is_admin(actor):
        raise PermissionDenied('Only administrators can change vendor status.')

    latest = vendor.applications.order_by('-submitted_at').first()

    if current == 'lead':
        if to == 'declined':
            set_qualification(vendor, Vendor.Qualification.NOT_QUALIFIED, actor=actor, reason=note)
        else:
            application = submit_application(actor=actor, vendor=vendor, source='Staff entry')
            if to == 'in_review' and application.stage == Stage.NEW:
                change_application_stage(application, Stage.IN_REVIEW, actor, reason=note)
    elif to == 'suspended' or current == 'suspended':
        _suspend_or_reinstate(vendor, suspend=to == 'suspended', actor=actor, note=note)
    elif current == 'declined' and (latest is None or vendor.journey_stage == Vendor.JourneyStage.LEAD):
        # A lead marked not qualified is reopened by resetting qualification and starting an application.
        set_qualification(vendor, Vendor.Qualification.NOT_ASSESSED, actor=actor, reason=note)
        application = submit_application(actor=actor, vendor=vendor, source='Staff entry')
        if application.stage == Stage.NEW:
            change_application_stage(application, Stage.IN_REVIEW, actor, reason=note)
    else:
        if latest is None:
            raise ValidationError({'status': 'This vendor has no application to update.'})
        change_application_stage(latest, DISPLAY_TO_STAGE[to], actor, reason=note)

    vendor.refresh_from_db()
    return vendor


def _suspend_or_reinstate(vendor: Vendor, *, suspend: bool, actor, note=''):
    current = vendor.display_status
    if suspend:
        previous = vendor.journey_stage
        vendor.journey_stage = Vendor.JourneyStage.SUSPENDED
    else:
        # Reinstate to whatever the vendor was before the latest suspension.
        suspension = vendor.activity.filter(
            event_type=Event.VENDOR_STATUS_CHANGED, metadata__to='suspended',
        ).order_by('-created_at').first()
        previous = Vendor.JourneyStage.SUSPENDED
        vendor.journey_stage = (suspension.metadata.get('previous_journey_stage') if suspension else None) \
            or Vendor.JourneyStage.APPROVED
    vendor.save(update_fields=['journey_stage'])
    to = 'suspended' if suspend else 'approved'
    record_activity(
        vendor, Event.VENDOR_STATUS_CHANGED, f'Vendor {current} → {vendor.display_status}', actor=actor,
        **{'from': current, 'to': to, 'note': note, 'previous_journey_stage': previous},
    )


# --- Notes -------------------------------------------------------------------

def add_note(vendor: Vendor, *, actor, body: str, note_type=InternalNote.NoteType.GENERAL, application=None):
    note = InternalNote.objects.create(
        vendor=vendor, application=application, author=actor, note_type=note_type, body=body
    )
    record_activity(
        vendor, Event.NOTE_ADDED, f'{note.get_note_type_display()} note added', actor=actor,
        application=application, note_id=str(note.pk),
    )
    return note
