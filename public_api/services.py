"""Business rules for the public application form. Submissions go through
vendors.services.submit_application like every other entry path; nothing here repeats pipeline rules."""

from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from catalog.models import MarketWeekend
from catalog.services import usage_for_weekends


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
