from rest_framework import serializers

from accounts.models import User
from accounts.serializers import UserSummarySerializer
from catalog.models import AddOn, BoothVariant
from tasks.models import Task

from .models import ActivityEvent, Application, ApplicationLine, InternalNote, Vendor
from .pipeline import application_totals, exhausted_calls
from .services import MatchResult

VENDOR_EDITABLE_FIELDS = [
    'brand_name', 'first_name', 'last_name', 'email', 'phone', 'instagram', 'website',
    'category', 'description', 'objectives', 'commitment_level', 'other_markets',
    'business_registration', 'liability_insurance', 'lead_source', 'source_details', 'tags', 'notes',
]
ADMIN_ONLY_VENDOR_FIELDS = {'owner', 'is_active', 'outreach_opt_out'}


class CallSummarySerializer(serializers.Serializer):
    """Latest discovery-call state, computed from the prefetched `discovery_calls`."""

    def to_representation(self, vendor):
        calls = getattr(vendor, 'discovery_calls', None)
        if calls is None:
            calls = list(vendor.tasks.filter(task_type=Task.TaskType.DISCOVERY_CALL).order_by('-created_at'))
        open_task = next((task for task in calls if task.is_open), None)
        attempted = [task for task in calls if task.attempt_count]
        latest = max(attempted, key=lambda task: task.last_attempt_at) if attempted else None
        return {
            'open_task_id': str(open_task.pk) if open_task else None,
            'open_task_status': open_task.status if open_task else None,
            'due_at': open_task.due_at if open_task else None,
            'owner_id': str(open_task.owner_id) if open_task and open_task.owner_id else None,
            'never_called': not attempted,
            'last_outcome': latest.outcome if latest else '',
            'last_called_at': latest.last_attempt_at if latest else None,
            'attempt_count': (open_task or latest).attempt_count if (open_task or latest) else 0,
            'call_details': latest.call_details if latest else {},
        }


class VendorSerializer(serializers.ModelSerializer):
    status = serializers.CharField(source='display_status', read_only=True)
    label = serializers.CharField(read_only=True)
    owner = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True), allow_null=True, required=False
    )
    owner_detail = UserSummarySerializer(source='owner', read_only=True)
    created_by = UserSummarySerializer(read_only=True)
    call = CallSummarySerializer(source='*', read_only=True)
    tags = serializers.ListField(child=serializers.CharField(max_length=50), required=False)

    class Meta:
        model = Vendor
        fields = [
            'id', 'created_at', 'updated_at', 'created_by', 'owner', 'owner_detail',
            *VENDOR_EDITABLE_FIELDS,
            'journey_stage', 'qualification_status', 'application_status_summary',
            'is_active', 'outreach_opt_out', 'status', 'label', 'call',
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'journey_stage', 'qualification_status', 'application_status_summary',
        ]
        extra_kwargs = {'brand_name': {'allow_blank': False}}

    def validate(self, attrs):
        user = self.context['request'].user
        if not getattr(user, 'is_administrator', False):
            changed = {
                key for key in ADMIN_ONLY_VENDOR_FIELDS & attrs.keys()
                if self.instance is None or getattr(self.instance, key) != attrs[key]
            }
            # Team members may set themselves as owner when creating a lead.
            if self.instance is None and attrs.get('owner') in (None, user):
                changed.discard('owner')
            if changed:
                raise serializers.ValidationError(
                    {key: 'Only administrators can change this.' for key in sorted(changed)}
                )
        brand = attrs.get('brand_name', getattr(self.instance, 'brand_name', ''))
        email = attrs.get('email', getattr(self.instance, 'email', ''))
        phone = attrs.get('phone', getattr(self.instance, 'phone', ''))
        instagram = attrs.get('instagram', getattr(self.instance, 'instagram', ''))
        if not brand.strip():
            raise serializers.ValidationError({'brand_name': 'Brand name is required.'})
        if not (email or phone or instagram):
            raise serializers.ValidationError({'contact': 'Add at least one of email, phone or Instagram.'})
        return attrs


class VendorSummarySerializer(serializers.ModelSerializer):
    status = serializers.CharField(source='display_status', read_only=True)

    class Meta:
        model = Vendor
        fields = ['id', 'brand_name', 'first_name', 'last_name', 'email', 'phone', 'instagram', 'status']


def match_payload(result: MatchResult) -> dict:
    return {
        'matches': [
            {'vendor': VendorSummarySerializer(m.vendor).data, 'matched_on': m.matched_on} for m in result.reliable
        ],
        'name_matches': VendorSummarySerializer(result.name_only, many=True).data,
    }


class VendorStatusChangeSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=['lead', 'applied', 'in_review', 'waitlisted', 'approved', 'declined', 'suspended'])
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class QualificationChangeSerializer(serializers.Serializer):
    qualification_status = serializers.ChoiceField(choices=Vendor.Qualification.choices)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ApplicationLineSerializer(serializers.ModelSerializer):
    variant = serializers.PrimaryKeyRelatedField(
        queryset=BoothVariant.objects.select_related('weekend'), allow_null=True, required=False,
    )
    add_on = serializers.PrimaryKeyRelatedField(
        queryset=AddOn.objects.select_related('weekend'), allow_null=True, required=False,
    )
    parent_line = serializers.PrimaryKeyRelatedField(
        queryset=ApplicationLine.objects.select_related('variant'), allow_null=True, required=False,
    )
    quantity = serializers.IntegerField(min_value=1, required=False, default=1)
    kind = serializers.SerializerMethodField()
    product_name = serializers.SerializerMethodField()
    weekend = serializers.SerializerMethodField()
    total_cents = serializers.IntegerField(read_only=True)

    class Meta:
        model = ApplicationLine
        fields = [
            'id', 'application', 'kind', 'variant', 'add_on', 'parent_line', 'product_name', 'weekend',
            'quantity', 'unit_price_cents', 'total_cents', 'status', 'status_reason',
            'waitlisted_at', 'confirmed_at', 'closed_at', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'application', 'unit_price_cents', 'status', 'status_reason',
            'waitlisted_at', 'confirmed_at', 'closed_at', 'created_at', 'updated_at',
        ]

    def get_kind(self, line):
        return 'booth' if line.variant_id else 'add_on'

    def get_product_name(self, line):
        return line.product.name

    def get_weekend(self, line):
        weekend = line.weekend
        return {'id': str(weekend.pk), 'label': weekend.label, 'start_date': weekend.start_date}


class SubmittedAddOnSerializer(serializers.Serializer):
    add_on = serializers.PrimaryKeyRelatedField(queryset=AddOn.objects.select_related('weekend'))
    quantity = serializers.IntegerField(min_value=1, default=1)


class SubmittedLineSerializer(serializers.Serializer):
    """A booth requested with a submission, optionally with add-ons for that booth."""
    variant = serializers.PrimaryKeyRelatedField(queryset=BoothVariant.objects.select_related('weekend'))
    quantity = serializers.IntegerField(min_value=1, default=1)
    add_ons = SubmittedAddOnSerializer(many=True, required=False, default=list)


class LineDecisionSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000, default='')


class PaymentNoteSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000, default='')


class ApplicationSerializer(serializers.ModelSerializer):
    vendor = serializers.PrimaryKeyRelatedField(queryset=Vendor.objects.all(), required=False)
    vendor_detail = VendorSummarySerializer(source='vendor', read_only=True)
    vendor_data = serializers.DictField(write_only=True, required=False)
    reviewer = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True), allow_null=True, required=False
    )
    submitted_by = UserSummarySerializer(read_only=True)
    # DEPRECATED (v1.3): derived from `stage` for the current frontend.
    status = serializers.CharField(source='derived_status', read_only=True)
    lines = ApplicationLineSerializer(many=True, read_only=True)
    requested_lines = SubmittedLineSerializer(many=True, write_only=True, required=False)
    notes = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=5000)
    call_exhausted = serializers.SerializerMethodField(
        help_text='The call task closed after the last allowed No Answer; what happens next is undecided.'
    )
    vendor_brand = serializers.CharField(source='vendor.brand_name', read_only=True)
    vendor_label = serializers.SerializerMethodField()
    has_waitlisted_lines = serializers.SerializerMethodField()
    call_attempts = serializers.SerializerMethodField()
    next_task_due_at = serializers.SerializerMethodField()
    requested_weekends = serializers.SerializerMethodField(help_text='Weekends the applicant asked for: [{id, label}].')
    campaign = serializers.SerializerMethodField(help_text="utm_campaign of the vendor's latest intake event, or blank.")

    class Meta:
        model = Application
        fields = [
            'id', 'vendor', 'vendor_brand', 'vendor_label', 'vendor_detail', 'vendor_data', 'requested_lines', 'notes',
            'submitted_by', 'source', 'submitted_at', 'stage', 'stage_entered_at',
            'has_waitlisted_lines', 'call_exhausted', 'call_attempts', 'next_task_due_at', 'status', 'legacy_status',
            'reviewer', 'category', 'product_description', 'social_snapshot', 'previous_market_experience',
            'booth_requirements', 'preferred_market_dates', 'required_support', 'responses',
            'lines', 'discount_type', 'discount_value', 'discount_label', 'billed_at', 'first_paid_at',
            'decision', 'decision_reason', 'decided_at', 'updated_at', 'requested_weekends', 'campaign',
        ]
        read_only_fields = [
            'id', 'submitted_by', 'submitted_at', 'social_snapshot', 'stage', 'stage_entered_at', 'legacy_status',
            'discount_type', 'discount_value', 'discount_label', 'billed_at', 'first_paid_at',
            'decision', 'decided_at', 'updated_at',
        ]

    def get_call_exhausted(self, application) -> bool:
        annotated = getattr(application, 'call_exhausted', None)
        if annotated is not None:
            return annotated
        return exhausted_calls().filter(application=application).exists()

    # The fallbacks below run only for an application not loaded through ApplicationViewSet.get_queryset.

    def get_vendor_label(self, application) -> str:
        return getattr(application, 'vendor_label', None) or application.vendor.label

    def get_has_waitlisted_lines(self, application) -> bool:
        annotated = getattr(application, 'has_waitlisted_lines', None)
        if annotated is not None:
            return annotated
        return any(line.status == ApplicationLine.Status.WAITLISTED for line in application.lines.all())

    def get_call_attempts(self, application) -> int:
        annotated = getattr(application, 'call_attempts', None)
        if annotated is not None:
            return annotated
        return sum(application.tasks.filter(task_type=Task.TaskType.DISCOVERY_CALL).values_list('attempt_count', flat=True))

    def get_next_task_due_at(self, application):
        if hasattr(application, 'next_task_due_at'):
            return application.next_task_due_at
        task = application.tasks.filter(status__in=Task.OPEN_STATUSES, due_at__isnull=False).order_by('due_at').first()
        return task.due_at if task else None

    def get_requested_weekends(self, application) -> list[dict]:
        return [{'id': str(weekend.pk), 'label': weekend.label} for weekend in application.requested_weekends.all()]

    def get_campaign(self, application) -> str:
        annotated = getattr(application, 'campaign', None)
        if annotated is not None:
            return annotated
        latest = application.vendor.intake_events.order_by('-captured_at').first()
        return str((latest.metadata or {}).get('utm_campaign') or '') if latest else ''

    def to_representation(self, application):
        """Adds currency, requested_total_cents, confirmed_subtotal_cents, discount_cents and
        confirmed_total_cents, computed from the (prefetched) lines."""
        data = super().to_representation(application)
        data.update(application_totals(application, list(application.lines.all())))
        return data

    def validate(self, attrs):
        if self.instance is None and not attrs.get('vendor') and not attrs.get('vendor_data'):
            raise serializers.ValidationError({'vendor': 'Provide an existing vendor id or vendor_data.'})
        if self.instance is not None and 'vendor' in attrs and attrs['vendor'] != self.instance.vendor:
            raise serializers.ValidationError({'vendor': 'An application cannot move to another vendor.'})
        return attrs


class ApplicationStatusChangeSerializer(serializers.Serializer):
    """DEPRECATED (v1.3): PATCH /applications/{id}/ with `status`; use the stage endpoint."""
    status = serializers.ChoiceField(choices=Application.Status.choices)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ApplicationStageChangeSerializer(serializers.Serializer):
    stage = serializers.ChoiceField(choices=Application.Stage.choices)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class DiscountSerializer(serializers.Serializer):
    discount_type = serializers.ChoiceField(choices=Application.DiscountType.choices)
    discount_value = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    discount_label = serializers.CharField(required=False, allow_blank=True, max_length=100, default='')


class InternalNoteSerializer(serializers.ModelSerializer):
    author = UserSummarySerializer(read_only=True)

    class Meta:
        model = InternalNote
        fields = ['id', 'vendor', 'application', 'author', 'note_type', 'body', 'created_at']
        read_only_fields = ['id', 'vendor', 'author', 'created_at']


class ActivityEventSerializer(serializers.ModelSerializer):
    actor = UserSummarySerializer(read_only=True)

    class Meta:
        model = ActivityEvent
        fields = ['id', 'vendor', 'application', 'task', 'event_type', 'description', 'actor', 'created_at', 'metadata']
        read_only_fields = fields
