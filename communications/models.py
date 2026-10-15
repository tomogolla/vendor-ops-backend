"""Email foundation (v1.3.1, Slice 1): settings, templates, immutable message records and
recipient preferences. Business rules live in services.py and sending.py."""

from datetime import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .rendering import VARIABLES, template_errors

GLOBAL_MODE_ORDER = {'disabled': 0, 'redirect': 1, 'enabled': 2}


class EmailSettings(models.Model):
    """The single site-wide email settings row (id=1). Only administrators may change it.

    The environment acts as a ceiling: the effective sending mode is the stricter of
    EMAIL_GLOBAL_MODE and `sending_mode`, and EMAIL_FROM_ADDRESS / EMAIL_REPLY_TO /
    EMAIL_REDIRECT_TO win over the values stored here when they are set."""

    class SendingMode(models.TextChoices):
        DISABLED = 'disabled', 'Disabled: nothing is sent'
        REDIRECT = 'redirect', 'Redirect: everything goes to the redirect address'
        ENABLED = 'enabled', 'Enabled: messages go to vendors'

    sender_name = models.CharField('default sender display name', max_length=100, default='The Good Flea')
    sender_email = models.EmailField('default sender email', blank=True)
    reply_to_email = models.EmailField('default reply-to email', blank=True)
    business_timezone = models.CharField(max_length=64, default='America/New_York')
    sending_mode = models.CharField(max_length=10, choices=SendingMode.choices, default=SendingMode.DISABLED)
    redirect_to_email = models.EmailField(
        'redirect recipient', blank=True, help_text='Every message goes here in redirect mode (a team mailbox).'
    )
    send_window_start = models.TimeField(default=time(9, 0))
    send_window_end = models.TimeField(default=time(18, 0))
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'email settings'
        verbose_name_plural = 'email settings'
        constraints = [models.CheckConstraint(condition=Q(id=1), name='email_settings_single_row')]

    def __str__(self):
        return 'Email settings'

    def clean(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        errors = {}
        try:
            ZoneInfo(self.business_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            errors['business_timezone'] = 'Use an IANA time zone name, e.g. America/New_York.'
        if self.send_window_start >= self.send_window_end:
            errors['send_window_end'] = 'The send window must end after it starts (same day).'
        if self.sending_mode == self.SendingMode.REDIRECT and not (self.redirect_to_email or settings.EMAIL_REDIRECT_TO):
            errors['redirect_to_email'] = 'Redirect mode needs a redirect recipient (here or EMAIL_REDIRECT_TO).'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> 'EmailSettings':
        """The settings row, created from the environment defaults the first time."""
        from datetime import time as clock

        def parse(value: str, fallback: time) -> time:
            try:
                return clock.fromisoformat(value)
            except (TypeError, ValueError):
                return fallback

        obj, _ = cls.objects.get_or_create(pk=1, defaults={
            'sender_name': settings.EMAIL_FROM_NAME,
            'business_timezone': settings.EMAIL_TIMEZONE,
            'send_window_start': parse(settings.EMAIL_SEND_WINDOW_START, time(9, 0)),
            'send_window_end': parse(settings.EMAIL_SEND_WINDOW_END, time(18, 0)),
        })
        return obj

    # --- Effective values (environment wins / is the ceiling) -------------------------------

    @property
    def effective_mode(self) -> str:
        env_mode = settings.EMAIL_GLOBAL_MODE if settings.EMAIL_GLOBAL_MODE in GLOBAL_MODE_ORDER else 'disabled'
        return min(env_mode, self.sending_mode, key=GLOBAL_MODE_ORDER.__getitem__)

    @property
    def effective_sender_email(self) -> str:
        return settings.EMAIL_FROM_ADDRESS or self.sender_email

    @property
    def effective_sender_name(self) -> str:
        return self.sender_name or settings.EMAIL_FROM_NAME

    @property
    def effective_reply_to(self) -> str:
        return settings.EMAIL_REPLY_TO or self.reply_to_email

    @property
    def effective_redirect_to(self) -> str:
        return settings.EMAIL_REDIRECT_TO or self.redirect_to_email


class EmailTemplate(models.Model):
    class Category(models.TextChoices):
        TRANSACTIONAL = 'transactional', 'Transactional'
        OPERATIONAL = 'operational', 'Operational'
        PROMOTIONAL = 'promotional', 'Promotional'

    class Mode(models.TextChoices):
        AUTO = 'auto', 'Auto: queued for the next send window'
        DRAFT = 'draft', 'Draft: a person reviews each message'
        OFF = 'off', 'Off: nothing is created'

    key = models.SlugField(max_length=64, unique=True, help_text='Stable identifier used by the code.')
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=Category.choices)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.OFF)
    subject_template = models.CharField(max_length=255)
    html_template = models.TextField('HTML body template')
    text_template = models.TextField('plain-text body template')
    allowed_variables = models.JSONField(
        default=list, blank=True,
        help_text=f'Placeholders this template may use. Available: {", ".join(VARIABLES)}.',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return f'{self.name} ({self.get_mode_display().split(":")[0]})'

    def clean(self):
        if not isinstance(self.allowed_variables, list) or not all(isinstance(v, str) for v in self.allowed_variables):
            raise ValidationError({'allowed_variables': 'Give a list of variable names.'})
        errors = template_errors(
            {'subject_template': self.subject_template, 'html_template': self.html_template,
             'text_template': self.text_template},
            self.allowed_variables,
        )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Unknown placeholders are rejected on every save, not only through forms.
        self.clean()
        super().save(*args, **kwargs)


class EmailMessage(models.Model):
    """One rendered message to one recipient. Content, recipient and sender are snapshots taken at
    queue time and never change; only the delivery fields move, through the services."""

    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        DRAFT = 'draft', 'Draft (awaiting review)'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'
        SKIPPED = 'skipped', 'Skipped'
        CANCELLED = 'cancelled', 'Cancelled'

    FINAL_STATUSES = (Status.SENT, Status.FAILED, Status.SKIPPED, Status.CANCELLED)
    SNAPSHOT_FIELDS = (
        'template_id', 'template_key', 'vendor_id', 'application_id', 'market_weekend_id', 'recipient_name', 'recipient_email',
        'sender_name', 'sender_email', 'reply_to_email', 'subject', 'html_body', 'text_body', 'dedupe_key',
        'event_key', 'created_by_id',
    )

    template = models.ForeignKey(EmailTemplate, null=True, blank=True, on_delete=models.PROTECT, related_name='messages')
    template_key = models.CharField(max_length=64, blank=True, help_text='Snapshot of the template key.')
    vendor = models.ForeignKey('vendors.Vendor', null=True, blank=True, on_delete=models.PROTECT, related_name='email_messages')
    application = models.ForeignKey(
        'vendors.Application', null=True, blank=True, on_delete=models.PROTECT, related_name='email_messages'
    )
    market_weekend = models.ForeignKey(
        'catalog.MarketWeekend', null=True, blank=True, on_delete=models.PROTECT, related_name='email_messages'
    )
    review_task = models.ForeignKey(
        'tasks.Task', null=True, blank=True, on_delete=models.SET_NULL, related_name='email_messages',
        help_text='The Review email task for a draft.',
    )

    recipient_name = models.CharField(max_length=200, blank=True)
    recipient_email = models.CharField(max_length=254, blank=True)
    sender_name = models.CharField(max_length=100, blank=True)
    sender_email = models.CharField(max_length=254, blank=True)
    reply_to_email = models.CharField(max_length=254, blank=True)
    subject = models.CharField(max_length=255)
    html_body = models.TextField()
    text_body = models.TextField()

    status = models.CharField(max_length=10, choices=Status.choices)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_to_email = models.CharField(
        max_length=254, blank=True, help_text='Where the provider actually delivered it (the redirect mailbox in redirect mode).'
    )
    redirected = models.BooleanField(default=False)
    failure_code = models.CharField(max_length=40, blank=True)
    failure_detail = models.CharField(max_length=500, blank=True, help_text='Safe diagnostic text; never credentials.')
    provider = models.CharField(max_length=40, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    dedupe_key = models.CharField(max_length=255, null=True, blank=True, unique=True)
    event_key = models.CharField(max_length=200, help_text='Identifier of the event that triggered the message.')

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'scheduled_for']),
            models.Index(fields=['vendor', '-created_at']),
            models.Index(fields=['application', '-created_at']),
            models.Index(fields=['market_weekend', '-created_at']),
            models.Index(fields=['template', '-created_at']),
            models.Index(fields=['provider_message_id']),
        ]

    def __str__(self):
        return f'{self.template_key or "email"} to {self.recipient_email or "(no address)"} ({self.status})'

    def save(self, *args, **kwargs):
        if not self._state.adding and self.pk:
            stored = type(self).objects.filter(pk=self.pk).values(*self.SNAPSHOT_FIELDS).first()
            if stored:
                changed = [name for name in self.SNAPSHOT_FIELDS if stored[name] != getattr(self, name)]
                if changed:
                    raise ValidationError(f'Email messages are immutable; cannot change {", ".join(changed)}.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Email messages are kept as history and cannot be deleted.')


class EmailRecipientPreference(models.Model):
    """Consent and suppression for one email address. A later slice adds the public unsubscribe page
    (which will look the row up by a signed token) and bounce/complaint webhooks."""

    class Consent(models.TextChoices):
        UNKNOWN = 'unknown', 'Unknown'
        GRANTED = 'granted', 'Granted'
        WITHDRAWN = 'withdrawn', 'Withdrawn'

    class SuppressionReason(models.TextChoices):
        UNSUBSCRIBE = 'unsubscribe', 'Unsubscribed'
        HARD_BOUNCE = 'hard_bounce', 'Hard bounce'
        COMPLAINT = 'complaint', 'Complaint'
        INVALID_ADDRESS = 'invalid_address', 'Invalid address'
        MANUAL = 'manual', 'Manual'

    email = models.CharField(max_length=254, unique=True, help_text='Stored lower-cased and trimmed.')
    vendor = models.ForeignKey(
        'vendors.Vendor', null=True, blank=True, on_delete=models.SET_NULL, related_name='email_preferences'
    )
    marketing_consent = models.CharField(max_length=10, choices=Consent.choices, default=Consent.UNKNOWN)
    transactional_permitted = models.BooleanField(default=True)
    suppressed = models.BooleanField(default=False)
    suppression_reason = models.CharField(max_length=20, choices=SuppressionReason.choices, blank=True)
    source = models.CharField(max_length=100, blank=True, help_text='Where the change came from, e.g. "admin".')
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['email']
        constraints = [
            models.CheckConstraint(
                condition=Q(suppressed=False) | ~Q(suppression_reason=''), name='suppression_needs_reason'
            ),
        ]

    def __str__(self):
        return f'{self.email}{" (suppressed)" if self.suppressed else ""}'

    def save(self, *args, **kwargs):
        self.email = (self.email or '').strip().lower()
        super().save(*args, **kwargs)
