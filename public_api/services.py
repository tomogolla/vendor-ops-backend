"""Business rules for the public application form. Submissions go through
vendors.services.submit_application like every other entry path; nothing here repeats pipeline rules."""

import logging
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.system_users import WEBSITE_FORM_NAME, website_form_user
from catalog.models import MarketWeekend
from catalog.services import usage_for_weekends
from tasks.services import open_duplicate_review
from vendors.models import Application
from vendors.services import AmbiguousVendorMatch, Intake, MatchResult, add_note, submit_application

logger = logging.getLogger(__name__)

SOURCE = WEBSITE_FORM_NAME  # Application.source, LeadIntakeEvent.source_detail and Vendor.lead_source
NOT_SURE = 'not_sure'
# Which reliable match a web submission attaches to when it matches several vendors.
MATCH_PRIORITY = ('email', 'phone', 'instagram')


def business_today():
    return timezone.localdate(timezone=ZoneInfo(settings.BUSINESS_HOURS['timezone']))


def open_weekends():
    """Weekends the public may apply for: OPEN and not over yet, soonest first."""
    return (
        MarketWeekend.objects.filter(status=MarketWeekend.Status.OPEN, end_date__gte=business_today())
        .prefetch_related('variants').order_by('start_date', 'label')
    )


def public_weekends() -> dict:
    """What the public page may show. Never prices, capacities, counts or vendor data: only
    whether a booth size is on the waitlist (no spot left)."""
    weekends = list(open_weekends())
    usage = usage_for_weekends(weekends)
    sizes: dict[str, str] = {}
    rows = []
    for weekend in weekends:
        variants = []
        for variant in weekend.variants.all():
            remaining = usage[variant.pk].remaining
            variants.append({'name': variant.name, 'dimensions': variant.dimensions, 'waitlist': remaining is not None and remaining <= 0})
            if not sizes.get(variant.name):
                sizes[variant.name] = variant.dimensions
        rows.append({
            'id': str(weekend.pk), 'label': weekend.label, 'start_date': weekend.start_date, 'end_date': weekend.end_date,
            'variants': variants,
            'waitlist_only': bool(variants) and all(variant['waitlist'] for variant in variants),
        })
    return {'weekends': rows, 'booth_sizes': [{'name': name, 'dimensions': dimensions} for name, dimensions in sizes.items()]}


def _preferred_match(result: MatchResult):
    """The vendor whose email matched; failing that, phone, then Instagram."""
    for field in MATCH_PRIORITY:
        for match in result.reliable:
            if field in match.matched_on:
                return match
    return result.reliable[0]


def _match_note(chosen, others) -> str:
    lines = [f'{" and ".join(m.matched_on).capitalize()} also matches {m.vendor.brand_name}' for m in others]
    return (
        f'Website application matched more than one vendor. It was added here because the '
        f'{" and ".join(chosen.matched_on)} matched. ' + '; '.join(lines) + '. Check whether these are the same vendor.'
    )


@transaction.atomic
def submit_public_application(data: dict, metadata: dict) -> Application:
    """One public submission: exactly what a staff-entered application produces (an application at
    New with a call task, or bundled into the vendor's open application), plus the weekends the
    applicant asked for. Nothing is saved if anything fails."""
    actor = website_form_user()
    weekends = data['weekends']
    size = data['booth_size']
    lines = []
    if size != NOT_SURE:
        for weekend in weekends:
            variant = next((v for v in weekend.variants.all() if v.name == size), None)
            if variant is not None:  # a weekend without that size is a requested weekend only
                lines.append({'variant': variant, 'quantity': 1})
    vendor_data = {
        'first_name': data['first_name'], 'last_name': data['last_name'], 'email': data['email'],
        'phone': data['phone'], 'brand_name': data['brand_name'], 'instagram': data['instagram'], 'lead_source': SOURCE,
    }
    # A matched vendor keeps its record; what this applicant typed is kept with the intake event so
    # the team can see new details (e.g. a changed phone number).
    metadata = {**metadata, 'submitted_contact': {key: vendor_data[key] for key in ('first_name', 'last_name', 'email', 'phone', 'brand_name', 'instagram')}}
    common = {
        'actor': actor, 'source': SOURCE, 'lines': lines, 'skip_duplicate_lines': True,
        'application_data': {'booth_requirements': {'size': 'not sure yet' if size == NOT_SURE else size}},
    }
    ambiguous = None
    try:
        application = submit_application(vendor_data=vendor_data, intake=Intake(source_detail=SOURCE, metadata=metadata), **common)
    except AmbiguousVendorMatch as error:
        # Never dropped and never revealed: attach to the email match and ask the team to check.
        ambiguous = error.result
        chosen = _preferred_match(ambiguous)
        others = [match for match in ambiguous.reliable if match is not chosen]
        intake = Intake(source_detail=SOURCE, metadata={
            **metadata, 'possible_duplicates': [str(match.vendor.pk) for match in others],
        })
        application = submit_application(vendor=chosen.vendor, intake=intake, **common)
    application.requested_weekends.add(*weekends)
    if ambiguous is not None:
        note = _match_note(chosen, others)
        add_note(chosen.vendor, actor=actor, body=note, application=application)
        open_duplicate_review(chosen.vendor, application, actor=actor, description=note)
    return application
