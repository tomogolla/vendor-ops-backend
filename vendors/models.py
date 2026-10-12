import uuid

from django.conf import settings
from django.db import models

from .normalization import normalize_email, normalize_instagram, normalize_phone


class Vendor(models.Model):
    """The permanent shared profile for a brand, from first lead onwards."""

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

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['journey_stage', 'is_active'])]

    def __str__(self):
        return self.brand_name

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
        """Single status used by the frontend lists and badges."""
        if self.journey_stage == self.JourneyStage.SUSPENDED:
            return 'suspended'
        if self.journey_stage == self.JourneyStage.APPROVED:
            return 'approved'
        if self.journey_stage == self.JourneyStage.APPLICANT:
            return {
                Application.Status.SUBMITTED: 'applied',
                Application.Status.IN_REVIEW: 'in_review',
                Application.Status.WAITLISTED: 'waitlisted',
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
    """A formal application. A vendor may apply more than once."""

    class Status(models.TextChoices):
        SUBMITTED = 'SUBMITTED', 'Submitted'
        IN_REVIEW = 'IN_REVIEW', 'In review'
        WAITLISTED = 'WAITLISTED', 'Waitlisted'
        APPROVED = 'APPROVED', 'Approved'
        DECLINED = 'DECLINED', 'Declined'
        WITHDRAWN = 'WITHDRAWN', 'Withdrawn'

    DECISION_STATUSES = {Status.APPROVED, Status.WAITLISTED, Status.DECLINED}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='applications')
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    source = models.CharField(max_length=100, blank=True, help_text='e.g. Staff entry, Application form')
    submitted_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)
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

    def __str__(self):
        return f'{self.vendor} ({self.get_status_display()})'


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
        APPLICATION_STATUS_CHANGED = 'APPLICATION_STATUS_CHANGED', 'Application status changed'
        VENDOR_STATUS_CHANGED = 'VENDOR_STATUS_CHANGED', 'Vendor status changed'
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
