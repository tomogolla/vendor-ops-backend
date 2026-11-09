"""Authenticated intake from Google Sheets form-submit triggers."""

import logging
import secrets

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.system_users import website_form_user
from tasks.services import open_duplicate_review
from vendors.models import InternalNote
from vendors.services import AmbiguousVendorMatch, Intake, add_note, submit_application

from .models import GoogleSheetsSubmission
from .text import clean_line

logger = logging.getLogger(__name__)
SOURCES = {'vendor_interest_form': 'Vendor Interest Form', 'popup_form': 'Popup form'}


class CleanField(serializers.CharField):
    def to_internal_value(self, data):
        value = clean_line(data, (self.max_length or 1000) + 1)
        if not value and not self.allow_blank:
            self.fail('blank')
        return value


class GoogleSheetsApplicationSerializer(serializers.Serializer):
    submission_id = CleanField(max_length=500)
    sheet_type = serializers.ChoiceField(choices=SOURCES)
    first_name = CleanField(max_length=100, required=False, allow_blank=True, default='')
    business_name = CleanField(max_length=200, required=False, allow_blank=True, default='')
    brand_name = CleanField(max_length=200, required=False, allow_blank=True, default='')
    phone_number = CleanField(max_length=50, required=False, allow_blank=True, default='')
    instagram = CleanField(max_length=100, required=False, allow_blank=True, default='')
    email = serializers.EmailField(max_length=254, required=False, allow_blank=True, default='')
    form_number = CleanField(max_length=100, required=False, allow_blank=True, default='')
    date_applied = CleanField(max_length=100, required=False, allow_blank=True, default='')
    submission_date = CleanField(max_length=100, required=False, allow_blank=True, default='')

    def validate(self, attrs):
        name = attrs['business_name'] if attrs['sheet_type'] == 'vendor_interest_form' else attrs['brand_name']
        if not name:
            raise serializers.ValidationError({'brand_name': 'A business or brand name is required.'})
        if not any(attrs[field] for field in ('email', 'phone_number', 'instagram')):
            raise serializers.ValidationError('At least one of email, phone number or Instagram is required.')
        attrs['brand_name'] = name
        attrs['email'] = attrs['email'].strip().lower()
        return attrs


def _chosen_match(result):
    for field in ('email', 'phone', 'instagram'):
        for match in result.reliable:
            if field in match.matched_on:
                return match
    return result.reliable[0]


def _submit(data):
    actor = website_form_user()
    source = SOURCES[data['sheet_type']]
    vendor_data = {
        'first_name': data['first_name'], 'brand_name': data['brand_name'], 'email': data['email'],
        'phone': data['phone_number'], 'instagram': data['instagram'], 'lead_source': source,
    }
    metadata = {
        'google_sheets_submission_id': data['submission_id'], 'sheet_type': data['sheet_type'],
        'form_number': data['form_number'], 'date_applied': data['date_applied'],
        'submission_date': data['submission_date'],
        'submitted_contact': {key: vendor_data[key] for key in ('first_name', 'email', 'phone', 'brand_name', 'instagram')},
    }
    common = {'actor': actor, 'source': source, 'intake': Intake(source_detail=source, metadata=metadata)}
    try:
        return submit_application(vendor_data=vendor_data, **common)
    except AmbiguousVendorMatch as error:
        chosen = _chosen_match(error.result)
        others = [match for match in error.result.reliable if match is not chosen]
        metadata['possible_duplicates'] = [str(match.vendor.pk) for match in others]
        common['intake'] = Intake(source_detail=source, metadata=metadata)
        application = submit_application(vendor=chosen.vendor, **common)
        details = '; '.join(f'{" and ".join(match.matched_on).capitalize()} also matches {match.vendor.brand_name}' for match in others)
        note = f'{source} submission matched more than one vendor. It was attached here because the {" and ".join(chosen.matched_on)} matched. {details}. Check whether these are the same vendor.'
        add_note(chosen.vendor, actor=actor, body=note, note_type=InternalNote.NoteType.APPLICATION_REVIEW, application=application)
        open_duplicate_review(chosen.vendor, application, actor=actor, description=note)
        return application


class GoogleSheetsApplicationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        configured = settings.GOOGLE_SHEETS_WEBHOOK_SECRET
        supplied = request.headers.get('X-Webhook-Secret', '')
        if not configured or not secrets.compare_digest(supplied, configured):
            return Response({'detail': 'Unauthorized.'}, status=status.HTTP_401_UNAUTHORIZED)
        serializer = GoogleSheetsApplicationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        existing = GoogleSheetsSubmission.objects.filter(submission_id=data['submission_id']).first()
        if existing:
            return Response({'status': 'already_received', 'application_id': str(existing.application_id)})
        try:
            with transaction.atomic():
                application = _submit(data)
                GoogleSheetsSubmission.objects.create(
                    submission_id=data['submission_id'], sheet_type=data['sheet_type'], application=application,
                )
        except IntegrityError:
            existing = GoogleSheetsSubmission.objects.get(submission_id=data['submission_id'])
            return Response({'status': 'already_received', 'application_id': str(existing.application_id)})
        logger.info('Google Sheets submission received: application %s', application.pk)
        return Response({'status': 'received', 'application_id': str(application.pk)}, status=status.HTTP_201_CREATED)
