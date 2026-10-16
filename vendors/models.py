import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .normalization import normalize_email, normalize_instagram, normalize_phone


def vendor_label_expression(prefix=''):
    """SQL for Vendor.label. `prefix` is the path to the vendor, e.g. 'vendor__' from Application."""
    vendor_ref = models.OuterRef(f'{prefix}pk' if prefix else 'pk')
    won = Application.objects.filter(vendor=vendor_ref, stage=Application.Stage.WON)
    return models.Case(
        models.When(**{f'{prefix}journey_stage': Vendor.JourneyStage.SUSPENDED},
                    then=models.Value(Vendor.Label.SUSPENDED)),
        models.When(**{f'{prefix}is_active': False}, then=models.Value(Vendor.Label.INACTIVE)),
        models.When(models.Exists(won), then=models.Value(Vendor.Label.ACTIVE)),
        default=models.Value(Vendor.Label.PROSPECT),
        output_field=models.CharField(),
    )


class VendorQuerySet(models.QuerySet):
    def with_label(self):
        """Annotates `label_value` (see Vendor.label) so lists can filter and sort on it."""
        return self.annotate(label_value=vendor_label_expression())


class Vendor(models.Model):
    """The permanent shared profile for a brand, from first lead onwards."""

    class Label(models.TextChoices):
        PROSPECT = 'prospect', 'Prospect'
        ACTIVE = 'active', 'Active'
        INACTIVE = 'inactive', 'Inactive'
        SUSPENDED = 'suspended', 'Suspended'

    class JourneyStage(models.TextChoices):
        LEAD = 'LEAD', 'Lead'
        APPLICANT = 'APPLICANT', 'Applicant'
        APPROVED = 'APPROVED', 'Approved vendor'
        SUSPENDED = 'SUSPENDED', 'Suspended'

    class Qualification(models.TextChoices):
        NOT_ASSESSED = 'NOT_ASSESSED', 'Not assessed'
        QUALIFIED = 'QUALIFIED', 'Qualified'
        NOT_QUALIFIED = 'NOT_QUALIFIED', 'Not qualified'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='owned_vendors'
    )

    brand_name = models.CharField(max_length=200)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    email_normalized = models.CharField(max_length=254, blank=True, db_index=True, editable=False)
    phone = models.CharField(max_length=50, blank=True)
    phone_normalized = models.CharField(max_length=30, blank=True, db_index=True, editable=False)
    instagram = models.CharField(max_length=100, blank=True)
    instagram_normalized = models.CharField(max_length=100, blank=True, db_index=True, editable=False)
    website = models.URLField(max_length=300, blank=True)

    category = models.CharField(max_length=100, blank=True)
    description = models.TextField('brand description', blank=True)
    objectives = models.TextField(blank=True)
    commitment_level = models.CharField(max_length=50, blank=True)
    other_markets = models.TextField(blank=True)
    business_registration = models.CharField(max_length=50, blank=True, default='Not answered')
    liability_insurance = models.CharField(max_length=50, blank=True, default='Not answered')

    lead_source = models.CharField(max_length=100, blank=True)
    source_details = models.CharField(max_length=300, blank=True)
    tags = models.JSONField(default=list, blank=True)
    notes = models.TextField('general internal notes', blank=True)

    # DEPRECATED (v1.3): journey and qualification are superseded by Application.stage and
    # Vendor.label. Kept (and still maintained) for the live frontend and rollback safety.
    journey_stage = models.CharField(max_length=20, choices=JourneyStage.choices, default=JourneyStage.LEAD)
    qualification_status = models.CharField(
        max_length=20, choices=Qualification.choices, default=Qualification.NOT_ASSESSED
    )
    application_status_summary = models.CharField(
        max_length=20, blank=True, help_text='Status of the most recent application, kept in sync by services.'
    )
    is_active = models.BooleanField(default=True)
    outreach_opt_out = models.BooleanField(
        default=False, help_text='Excluded from outreach: no automatic call tasks.'
    )

    objects = VendorQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['journey_stage', 'is_active'])]

    def __str__(self):
        return self.brand_name

    @property
    def label(self) -> str:
        """suspended, inactive, active (any application WON) or prospect."""
        annotated = getattr(self, 'label_value', None)
        if annotated is not None:
            return annotated
        if self.journey_stage == self.JourneyStage.SUSPENDED:
            return self.Label.SUSPENDED
        if not self.is_active:
            return self.Label.INACTIVE
        if self.applications.filter(stage=Application.Stage.WON).exists():
            return self.Label.ACTIVE
        return self.Label.PROSPECT

    def save(self, *args, **kwargs):
        self.email_normalized = normalize_email(self.email)
        self.phone_normalized = normalize_phone(self.phone)
        self.instagram_normalized = normalize_instagram(self.instagram)
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            kwargs['update_fields'] = set(update_fields) | {
                'email_normalized', 'phone_normalized', 'instagram_normalized', 'updated_at'
            }
        super().save(*args, **kwargs)

    @property
    def display_status(self) -> str:
        """DEPRECATED (v1.3): single status read by the current frontend lists and badges.
        `application_status_summary` is derived from the latest application's stage, so this
        maps NEW -> applied, IN_REVIEW -> in_review, AWAITING_PAYMENT/WON -> approved and
        DECLINED/WITHDRAWN -> declined."""
        if self.journey_stage == self.JourneyStage.SUSPENDED:
            return 'suspended'
        if self.journey_stage == self.JourneyStage.APPROVED:
            return 'approved'
        if self.journey_stage == self.JourneyStage.APPLICANT:
            return {
                Application.Status.SUBMITTED: 'applied',
                Application.Status.IN_REVIEW: 'in_review',
                # Waitlisting is per line since v1.3; a pre-migration summary reads as in review.
                Application.Status.WAITLISTED: 'in_review',
                Application.Status.APPROVED: 'approved',
                Application.Status.DECLINED: 'declined',
                Application.Status.WITHDRAWN: 'declined',
            }.get(self.application_status_summary, 'applied')
        if self.qualification_status == self.Qualification.NOT_QUALIFIED:
            return 'declined'
        return 'lead'


class LeadIntakeEvent(models.Model):
    """How a vendor entered (or re-entered) the system."""

    class SourceType(models.TextChoices):
        MANUAL_REGISTRATION = 'MANUAL_REGISTRATION', 'Manual registration'
        CSV_IMPORT = 'CSV_IMPORT', 'CSV import'
        APPLICATION_SUBMISSION = 'APPLICATION_SUBMISSION', 'Application submission'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='intake_events')
    source_type = models.CharField(max_length=30, choices=SourceType.choices)
    source_detail = models.CharField(max_length=300, blank=True)
    import_batch = models.ForeignKey(
        'imports.ImportBatch', null=True, blank=True, on_delete=models.SET_NULL, related_name='intake_events'
    )
    captured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    captured_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-captured_at']

    def __str__(self):
        return f'{self.get_source_type_display()}: {self.vendor}'


class Application(models.Model):
    """A vendor's order for booths, moving through the sales pipeline. A vendor may apply more than once."""

    class Status(models.TextChoices):
        """DEPRECATED (v1.3): replaced by Stage. Kept for the live frontend and for rollback safety."""
        SUBMITTED = 'SUBMITTED', 'Submitted'
        IN_REVIEW = 'IN_REVIEW', 'In review'
        WAITLISTED = 'WAITLISTED', 'Waitlisted'
        APPROVED = 'APPROVED', 'Approved'
        DECLINED = 'DECLINED', 'Declined'
        WITHDRAWN = 'WITHDRAWN', 'Withdrawn'

    class Stage(models.TextChoices):
        NEW = 'NEW', 'New'
        IN_REVIEW = 'IN_REVIEW', 'In review'
        AWAITING_PAYMENT = 'AWAITING_PAYMENT', 'Awaiting payment'
        WON = 'WON', 'Won'
        # Provisional terminal stages; see TODO(decision) in vendors/pipeline.py.
        DECLINED = 'DECLINED', 'Declined'
        WITHDRAWN = 'WITHDRAWN', 'Withdrawn'

    class DiscountType(models.TextChoices):
        NONE = 'NONE', 'None'
        PERCENT = 'PERCENT', 'Percent'
        FIXED = 'FIXED', 'Fixed amount'

    # DEPRECATED (v1.3): what the old `status` field reports for each stage.
    STAGE_TO_STATUS = {
        Stage.NEW: Status.SUBMITTED,
        Stage.IN_REVIEW: Status.IN_REVIEW,
        Stage.AWAITING_PAYMENT: Status.APPROVED,
        Stage.WON: Status.APPROVED,
        Stage.DECLINED: Status.DECLINED,
        Stage.WITHDRAWN: Status.WITHDRAWN,
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='applications')
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    source = models.CharField(max_length=100, blank=True, help_text='e.g. Staff entry, Application form')
    submitted_at = models.DateTimeField(auto_now_add=True)
    # DEPRECATED (v1.3): kept in step with `stage` by vendors.pipeline; read `stage` instead.
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)

    # db_default keeps inserts from older code working if a deploy is rolled back.
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.NEW, db_default=Stage.NEW)
    stage_entered_at = models.DateTimeField(null=True, blank=True, default=timezone.now)
    legacy_status = models.CharField(
        max_length=20, null=True, blank=True, choices=Status.choices,
        help_text='The status this application had when it was migrated to stages (v1.3).',
    )
    discount_type = models.CharField(
        max_length=10, choices=DiscountType.choices, default=DiscountType.NONE, db_default=DiscountType.NONE
    )
    discount_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, db_default=0,
        help_text='Percent (0-100) for PERCENT, whole cents for FIXED.',
    )
    discount_label = models.CharField(max_length=100, blank=True, default='', db_default='')
    billed_at = models.DateTimeField(null=True, blank=True)
    first_paid_at = models.DateTimeField(null=True, blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewing_applications'
    )

    category = models.CharField(max_length=100, blank=True)
    product_description = models.TextField(blank=True)
    social_snapshot = models.JSONField(default=dict, blank=True, help_text='Website/social details at submission.')
    previous_market_experience = models.TextField(blank=True)
    booth_requirements = models.JSONField(default=dict, blank=True, help_text='e.g. {"size": "8ft x 8ft", "needs_tent": false}')
    preferred_market_dates = models.JSONField(default=list, blank=True)
    required_support = models.TextField(blank=True)
    responses = models.JSONField(default=dict, blank=True, help_text='Flexible structured answers.')

    decision = models.CharField(max_length=20, blank=True, choices=Status.choices)
    decision_reason = models.TextField(blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-submitted_at']
        indexes = [models.Index(fields=['stage', 'stage_entered_at'])]
        constraints = [
            models.CheckConstraint(
                condition=Q(discount_value__gte=0) & (~Q(discount_type='PERCENT') | Q(discount_value__lte=100)),
                name='application_discount_in_range',
            ),
        ]

    def __str__(self):
        return f'{self.vendor} ({self.get_stage_display()})'

    @property
    def derived_status(self) -> str:
        """DEPRECATED (v1.3): the old status value, derived from the stage."""
        return self.STAGE_TO_STATUS.get(self.stage, self.status)


class ApplicationLine(models.Model):
    """One booth, or one add-on for a booth line, requested on an application. Rules live in vendors.pipeline."""

    class Status(models.TextChoices):
        REQUESTED = 'REQUESTED', 'Requested'
        CONFIRMED = 'CONFIRMED', 'Confirmed'
        WAITLISTED = 'WAITLISTED', 'Waitlisted'
        DECLINED = 'DECLINED', 'Declined'
        CANCELLED = 'CANCELLED', 'Cancelled'

    OPEN_STATUSES = (Status.REQUESTED, Status.WAITLISTED, Status.CONFIRMED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name='lines')
    variant = models.ForeignKey(
        'catalog.BoothVariant', null=True, blank=True, on_delete=models.PROTECT, related_name='lines'
    )
    add_on = models.ForeignKey('catalog.AddOn', null=True, blank=True, on_delete=models.PROTECT, related_name='lines')
    parent_line = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.PROTECT, related_name='add_on_lines',
        help_text='The booth line an add-on line belongs to.',
    )
    quantity = models.PositiveIntegerField(default=1)
    unit_price_cents = models.PositiveIntegerField(help_text='Price copied from the product when the line was added.')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)
    status_reason = models.TextField(blank=True)
    waitlisted_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.CheckConstraint(
                condition=Q(variant__isnull=False, add_on__isnull=True) | Q(variant__isnull=True, add_on__isnull=False),
                name='line_has_exactly_one_product',
            ),
            models.CheckConstraint(
                condition=Q(add_on__isnull=True) | Q(parent_line__isnull=False), name='add_on_line_has_parent',
            ),
            models.CheckConstraint(
                condition=Q(variant__isnull=True) | Q(parent_line__isnull=True), name='booth_line_has_no_parent',
            ),
            models.CheckConstraint(condition=Q(quantity__gte=1), name='line_quantity_at_least_one'),
        ]
        indexes = [
            models.Index(fields=['variant', 'status']),
            models.Index(fields=['add_on', 'status']),
            models.Index(fields=['status', 'waitlisted_at']),
        ]

    def __str__(self):
        return f'{self.product} ×{self.quantity} ({self.get_status_display()})'

    @property
    def product(self):
        return self.variant if self.variant_id else self.add_on

    @property
    def weekend(self):
        return self.product.weekend

    @property
    def total_cents(self) -> int:
        return self.quantity * self.unit_price_cents


class InternalNote(models.Model):
    class NoteType(models.TextChoices):
        LEAD_INTAKE = 'LEAD_INTAKE', 'Lead intake'
        DISCOVERY_CALL = 'DISCOVERY_CALL', 'Discovery call'
        QUALIFICATION = 'QUALIFICATION', 'Qualification'
        APPLICATION_REVIEW = 'APPLICATION_REVIEW', 'Application review'
        GENERAL = 'GENERAL', 'General'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='internal_notes')
    application = models.ForeignKey(
        Application, null=True, blank=True, on_delete=models.SET_NULL, related_name='internal_notes'
    )
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    note_type = models.CharField(max_length=30, choices=NoteType.choices, default=NoteType.GENERAL)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class ActivityEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise TypeError('Activity events are immutable.')

    def delete(self):
        raise TypeError('Activity events are immutable.')


class ActivityEvent(models.Model):
    """Append-only vendor timeline. Rows are never edited or deleted through the app."""

    class EventType(models.TextChoices):
        LEAD_REGISTERED = 'LEAD_REGISTERED', 'Lead registered'
        LEAD_IMPORTED = 'LEAD_IMPORTED', 'Lead imported'
        APPLICATION_SUBMITTED = 'APPLICATION_SUBMITTED', 'Application submitted'
        TASK_CREATED = 'TASK_CREATED', 'Task created'
        CALL_ATTEMPTED = 'CALL_ATTEMPTED', 'Call attempted'
        CALL_COMPLETED = 'CALL_COMPLETED', 'Call completed'
        QUALIFICATION_CHANGED = 'QUALIFICATION_CHANGED', 'Qualification changed'
        APPLICATION_STATUS_CHANGED = 'APPLICATION_STATUS_CHANGED', 'Application status changed'  # pre-v1.3 history
        APPLICATION_STAGE_CHANGED = 'APPLICATION_STAGE_CHANGED', 'Application stage changed'
        APPLICATION_BUNDLED = 'APPLICATION_BUNDLED', 'Submission bundled into open application'
        APPLICATION_BILLED = 'APPLICATION_BILLED', 'Application billed'
        APPLICATION_PAYMENT_RECEIVED = 'APPLICATION_PAYMENT_RECEIVED', 'First payment received'
        APPLICATION_DISCOUNT_CHANGED = 'APPLICATION_DISCOUNT_CHANGED', 'Application discount changed'
        LINE_ADDED = 'LINE_ADDED', 'Line added'
        LINE_UPDATED = 'LINE_UPDATED', 'Line updated'
        LINE_REMOVED = 'LINE_REMOVED', 'Line removed'
        LINE_CONFIRMED = 'LINE_CONFIRMED', 'Line confirmed'
        LINE_WAITLISTED = 'LINE_WAITLISTED', 'Line waitlisted'
        LINE_DECLINED = 'LINE_DECLINED', 'Line declined'
        LINE_CANCELLED = 'LINE_CANCELLED', 'Line cancelled'
        LINE_RELEASED = 'LINE_RELEASED', 'Line hold released'
        VENDOR_STATUS_CHANGED = 'VENDOR_STATUS_CHANGED', 'Vendor status changed'
        EMAIL_QUEUED = 'EMAIL_QUEUED', 'Email queued'
        EMAIL_DRAFTED = 'EMAIL_DRAFTED', 'Email drafted for review'
        EMAIL_SENT = 'EMAIL_SENT', 'Email sent'
        EMAIL_FAILED = 'EMAIL_FAILED', 'Email failed'
        EMAIL_SKIPPED = 'EMAIL_SKIPPED', 'Email skipped'
        EMAIL_CANCELLED = 'EMAIL_CANCELLED', 'Email cancelled'
        EMAIL_BOUNCED = 'EMAIL_BOUNCED', 'Email bounced'
        EMAIL_COMPLAINED = 'EMAIL_COMPLAINED', 'Email marked as spam'
        NOTE_ADDED = 'NOTE_ADDED', 'Note added'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='activity')
    application = models.ForeignKey(
        Application, null=True, blank=True, on_delete=models.PROTECT, related_name='activity'
    )
    task = models.ForeignKey('tasks.Task', null=True, blank=True, on_delete=models.PROTECT, related_name='activity')
    event_type = models.CharField(max_length=40, choices=EventType.choices)
    description = models.CharField(max_length=500)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
        help_text='Null for system actions.',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = ActivityEventQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_event_type_display()}: {self.description}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError('Activity events are immutable.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError('Activity events are immutable.')
