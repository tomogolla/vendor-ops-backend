"""Validation for the public application form. Errors only ever describe the applicant's own input,
never anything about existing vendors or applications."""

import re

from rest_framework import serializers

from vendors.normalization import normalize_instagram, normalize_phone

from .services import NOT_SURE, open_weekends
from .text import clean_line

MAX_WEEKENDS = 12
INSTAGRAM_HANDLE = re.compile(r'^[a-z0-9._]{1,30}$')


class CleanCharField(serializers.CharField):
    """A single line of text with control characters stripped and whitespace collapsed."""

    def to_internal_value(self, data):
        if not isinstance(data, (str, int, float)) or isinstance(data, bool):
            self.fail('invalid')
        value = clean_line(data, (self.max_length or 1000) + 1)
        if not value and not self.allow_blank:
            self.fail('blank')
        return value


class PublicApplicationSerializer(serializers.Serializer):
    first_name = CleanCharField(max_length=100)
    last_name = CleanCharField(max_length=100)
    email = serializers.EmailField(max_length=254)
    phone = CleanCharField(max_length=40)
    brand_name = CleanCharField(max_length=200)
    instagram = CleanCharField(max_length=200)
    weekend_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False, max_length=50)
    booth_size = CleanCharField(max_length=100)
    consent = serializers.BooleanField()
    consent_text_version = CleanCharField(max_length=50, required=False, allow_blank=True, default='')
    turnstile_token = serializers.CharField(max_length=2048, required=False, allow_blank=True, default='', trim_whitespace=True)
    # Honeypot: checked by the view before validation; anything here means a bot.
    website = serializers.CharField(required=False, allow_blank=True, default='')
    attribution = serializers.DictField(required=False, default=dict)

    default_error_messages = {'weekends_closed': 'One or more of these market weekends is not open for applications.'}

    def validate_email(self, value):
        return value.strip().lower()

    def validate_phone(self, value):
        digits = normalize_phone(value)
        if not 7 <= len(digits) <= 15:
            raise serializers.ValidationError('Enter a phone number with 7 to 15 digits.')
        return value

    def validate_instagram(self, value):
        handle = normalize_instagram(value)
        if not INSTAGRAM_HANDLE.match(handle):
            raise serializers.ValidationError(
                'Enter your Instagram handle (letters, numbers, dots and underscores, up to 30 characters) or profile link.'
            )
        return handle

    def validate_weekend_ids(self, value):
        distinct = list(dict.fromkeys(value))
        if len(distinct) > MAX_WEEKENDS:
            raise serializers.ValidationError(f'Choose at most {MAX_WEEKENDS} market weekends.')
        return distinct

    def validate_consent(self, value):
        if value is not True:
            raise serializers.ValidationError('Please agree to be contacted about your application.')
        return value

    def validate(self, attrs):
        ids = attrs['weekend_ids']
        weekends = {str(weekend.pk): weekend for weekend in open_weekends().filter(pk__in=ids)}
        if len(weekends) != len(ids):
            raise serializers.ValidationError({'weekend_ids': self.error_messages['weekends_closed']})
        attrs['weekends'] = [weekends[str(pk)] for pk in ids]
        size = attrs['booth_size']
        if size != NOT_SURE and not any(variant.name == size for weekend in attrs['weekends'] for variant in weekend.variants.all()):
            raise serializers.ValidationError({'booth_size': 'Choose a booth size offered on one of your market weekends, or "not sure yet".'})
        return attrs
