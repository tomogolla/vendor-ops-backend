import uuid

from django.db import models
from django.db.models import Q


class MarketWeekend(models.Model):
    """One market weekend that vendors book booths for."""

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        OPEN = 'OPEN', 'Open'
        CLOSED = 'CLOSED', 'Closed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label = models.CharField(max_length=100, help_text="e.g. 'Oct 10 & 11'")
    start_date = models.DateField()
    end_date = models.DateField()
    venue = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['start_date', 'label']
        constraints = [
            models.CheckConstraint(condition=Q(end_date__gte=models.F('start_date')), name='weekend_end_after_start'),
        ]

    def __str__(self):
        return self.label


class BoothVariant(models.Model):
    """A booth size sold for one weekend, with a fixed number of spots."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    weekend = models.ForeignKey(MarketWeekend, on_delete=models.CASCADE, related_name='variants')
    name = models.CharField(max_length=100, help_text="e.g. '8ft x 8ft'")
    dimensions = models.CharField(max_length=100, blank=True)
    price_cents = models.PositiveIntegerField()
    capacity = models.PositiveIntegerField(help_text='Number of booths of this size.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['weekend', 'price_cents', 'name']
        constraints = [models.UniqueConstraint(fields=['weekend', 'name'], name='unique_variant_name_per_weekend')]

    def __str__(self):
        return f'{self.weekend}: {self.name}'


class AddOn(models.Model):
    """An extra sold with a booth for one weekend, e.g. tent rental."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    weekend = models.ForeignKey(MarketWeekend, on_delete=models.CASCADE, related_name='add_ons')
    name = models.CharField(max_length=100, help_text="e.g. 'Tent rental'")
    price_cents = models.PositiveIntegerField()
    capacity = models.PositiveIntegerField(null=True, blank=True, help_text='Empty means unlimited.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['weekend', 'name']
        constraints = [models.UniqueConstraint(fields=['weekend', 'name'], name='unique_add_on_name_per_weekend')]

    def __str__(self):
        return f'{self.weekend}: {self.name}'
