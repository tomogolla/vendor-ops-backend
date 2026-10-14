"""Catalog rules. Capacity usage is always computed from application lines, never stored:
only CONFIRMED lines consume capacity, and they hold it until released."""

from dataclasses import dataclass, field

from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from rest_framework.exceptions import ValidationError

from vendors.models import ApplicationLine

from .models import AddOn, BoothVariant, MarketWeekend

LineStatus = ApplicationLine.Status
SUGGESTIONS_PER_PRODUCT = 5


@dataclass
class Usage:
    capacity: int | None
    confirmed: int = 0
    waitlisted: int = 0
    suggestions: list[ApplicationLine] = field(default_factory=list)

    @property
    def remaining(self) -> int | None:
        return None if self.capacity is None else self.capacity - self.confirmed


def _product_field(product) -> str:
    return 'variant' if isinstance(product, BoothVariant) else 'add_on'


def confirmed_quantity(product) -> int:
    lines = ApplicationLine.objects.filter(**{_product_field(product): product}, status=LineStatus.CONFIRMED)
    return lines.aggregate(total=Coalesce(Sum('quantity'), 0))['total']


def usage_for_weekends(weekends: list[MarketWeekend]) -> dict:
    """Usage keyed by variant or add-on id, for every product of the given weekends, with
    promotion suggestions: the oldest waitlisted lines of products that have room. Suggestions
    only; nothing is promoted automatically."""
    variants = BoothVariant.objects.filter(weekend__in=weekends)
    add_ons = AddOn.objects.filter(weekend__in=weekends)
    usage = {product.pk: Usage(product.capacity) for product in [*variants, *add_ons]}

    lines = ApplicationLine.objects.filter(
        Q(variant__weekend__in=weekends) | Q(add_on__weekend__in=weekends),
        status__in=[LineStatus.CONFIRMED, LineStatus.WAITLISTED],
    )
    for row in lines.values('variant_id', 'add_on_id', 'status').annotate(quantity=Sum('quantity'), count=Count('id')):
        product_usage = usage[row['variant_id'] or row['add_on_id']]
        if row['status'] == LineStatus.CONFIRMED:
            product_usage.confirmed = row['quantity']
        else:
            product_usage.waitlisted = row['count']

    with_room = [pk for pk, item in usage.items() if item.waitlisted and (item.remaining is None or item.remaining > 0)]
    if with_room:
        waitlisted = ApplicationLine.objects.filter(
            Q(variant__in=with_room) | Q(add_on__in=with_room), status=LineStatus.WAITLISTED,
        ).select_related('application__vendor').order_by('waitlisted_at', 'created_at')
        for line in waitlisted:
            suggestions = usage[line.variant_id or line.add_on_id].suggestions
            if len(suggestions) < SUGGESTIONS_PER_PRODUCT:
                suggestions.append(line)
    return usage


def check_capacity_change(product, capacity: int | None):
    """Capacity may not drop below what is already confirmed."""
    if capacity is None:
        return
    type(product).objects.select_for_update().get(pk=product.pk)
    confirmed = confirmed_quantity(product)
    if capacity < confirmed:
        raise ValidationError({'capacity': f'{confirmed} already confirmed; release holds before lowering capacity below that.'})


def delete_product(product):
    if product.lines.exists():
        raise ValidationError({'detail': 'This product has application lines and cannot be deleted.'})
    product.delete()


def delete_weekend(weekend: MarketWeekend):
    if ApplicationLine.objects.filter(Q(variant__weekend=weekend) | Q(add_on__weekend=weekend)).exists():
        raise ValidationError({'detail': 'This weekend has application lines and cannot be deleted.'})
    weekend.delete()
