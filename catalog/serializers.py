from django.conf import settings
from rest_framework import serializers

from .models import AddOn, BoothVariant, MarketWeekend


class UsageMixin(serializers.Serializer):
    """Capacity counts read from `context['usage']` (see services.usage_for_weekends)."""

    confirmed = serializers.SerializerMethodField()
    remaining = serializers.SerializerMethodField()
    waitlisted = serializers.SerializerMethodField()
    promotion_suggestions = serializers.SerializerMethodField(
        help_text='Oldest waitlisted lines while there is room. Suggestions only; confirm them by hand.'
    )

    def _usage(self, product):
        return self.context.get('usage', {}).get(product.pk)

    def get_promotion_suggestions(self, product):
        usage = self._usage(product)
        return [
            {
                'line_id': str(line.pk), 'application_id': str(line.application_id),
                'vendor_id': str(line.application.vendor_id), 'brand_name': line.application.vendor.brand_name,
                'quantity': line.quantity, 'waitlisted_at': line.waitlisted_at,
                'fits': usage.remaining is None or line.quantity <= usage.remaining,
            }
            for line in (usage.suggestions if usage else [])
        ]

    def get_confirmed(self, product):
        usage = self._usage(product)
        return usage.confirmed if usage else 0

    def get_remaining(self, product):
        usage = self._usage(product)
        return usage.remaining if usage else product.capacity

    def get_waitlisted(self, product):
        usage = self._usage(product)
        return usage.waitlisted if usage else 0


class BoothVariantSerializer(UsageMixin, serializers.ModelSerializer):
    class Meta:
        model = BoothVariant
        fields = [
            'id', 'weekend', 'name', 'dimensions', 'price_cents', 'capacity',
            'confirmed', 'remaining', 'waitlisted', 'promotion_suggestions', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'weekend', 'created_at', 'updated_at']
        # Uniqueness per weekend is checked in validate(); the weekend comes from the URL.
        validators = []

    def validate_name(self, value):
        weekend = self.context['weekend']
        clash = BoothVariant.objects.filter(weekend=weekend, name__iexact=value.strip())
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError('This weekend already has a booth variant with this name.')
        return value.strip()


class AddOnSerializer(UsageMixin, serializers.ModelSerializer):
    class Meta:
        model = AddOn
        fields = [
            'id', 'weekend', 'name', 'price_cents', 'capacity',
            'confirmed', 'remaining', 'waitlisted', 'promotion_suggestions', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'weekend', 'created_at', 'updated_at']
        validators = []

    def validate_name(self, value):
        weekend = self.context['weekend']
        clash = AddOn.objects.filter(weekend=weekend, name__iexact=value.strip())
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError('This weekend already has an add-on with this name.')
        return value.strip()


class MarketWeekendSerializer(serializers.ModelSerializer):
    variants = BoothVariantSerializer(many=True, read_only=True)
    add_ons = AddOnSerializer(many=True, read_only=True)
    currency = serializers.SerializerMethodField()

    class Meta:
        model = MarketWeekend
        fields = [
            'id', 'label', 'start_date', 'end_date', 'venue', 'status', 'currency',
            'variants', 'add_ons', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_currency(self, weekend):
        return settings.CURRENCY

    def validate(self, attrs):
        start = attrs.get('start_date', getattr(self.instance, 'start_date', None))
        end = attrs.get('end_date', getattr(self.instance, 'end_date', None))
        if start and end and end < start:
            raise serializers.ValidationError({'end_date': 'The weekend cannot end before it starts.'})
        return attrs
