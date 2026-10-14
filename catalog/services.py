"""Catalog rules. Capacity usage is always computed from application lines, never stored."""

from dataclasses import dataclass

from .models import AddOn, BoothVariant, MarketWeekend


@dataclass
class Usage:
    capacity: int | None
    confirmed: int = 0
    waitlisted: int = 0

    @property
    def remaining(self) -> int | None:
        return None if self.capacity is None else self.capacity - self.confirmed


def usage_for_weekends(weekends: list[MarketWeekend]) -> dict:
    """Usage keyed by variant or add-on id, for every product of the given weekends."""
    variants = BoothVariant.objects.filter(weekend__in=weekends)
    add_ons = AddOn.objects.filter(weekend__in=weekends)
    # Lines arrive in step 3 of the v1.3 pipeline; until then nothing is confirmed or waitlisted.
    return {product.pk: Usage(product.capacity) for product in [*variants, *add_ons]}


def check_capacity_change(product, capacity: int | None):
    """Capacity may not drop below what is already confirmed."""


def delete_product(product):
    product.delete()


def delete_weekend(weekend: MarketWeekend):
    weekend.delete()
