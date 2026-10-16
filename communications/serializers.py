from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from accounts.serializers import UserSummarySerializer
from catalog.models import MarketWeekend
from vendors.models import ApplicationLine

from . import services, stop_conditions
from .models import EmailDraftRevision, EmailMessage, EmailSettings, EmailTemplate
from .rendering import VARIABLES


def _django_errors(error: DjangoValidationError) -> dict | list:
    return error.message_dict if hasattr(error, 'error_dict') else error.messages


# --- Settings ------------------------------------------------------------------------------------

class EmailSettingsSerializer(serializers.ModelSerializer):
    """Stored settings plus what is actually in effect. Credentials are never included."""

    updated_by = UserSummarySerializer(read_only=True)

    class Meta:
        model = EmailSettings
        fields = [
            'sending_mode', 'redirect_to_email', 'sender_name', 'sender_email', 'reply_to_email',
            'business_timezone', 'send_window_start', 'send_window_end', 'updated_by', 'updated_at',
        ]
        read_only_fields = ['updated_by', 'updated_at']

    def validate(self, attrs):
        candidate = EmailSettings(**{**{f.name: getattr(self.instance, f.name) for f in EmailSettings._meta.fields}, **attrs})
        try:
            candidate.clean()
        except DjangoValidationError as error:
            raise serializers.ValidationError(_django_errors(error)) from None
        return attrs

    def to_representation(self, instance):
        from .sending import daily_remaining

        data = super().to_representation(instance)
        provider = settings.EMAIL_PROVIDER
        provider_configured = {
            'console': True,
            'smtp_relay': bool(settings.EMAIL_FROM_ADDRESS),
            'resend': bool(settings.ANYMAIL.get('RESEND_API_KEY') and settings.EMAIL_FROM_ADDRESS),
        }.get(provider, False)
        effective = instance.effective_mode
        notices = []
        if settings.EMAIL_GLOBAL_MODE != 'enabled' and effective != instance.sending_mode:
            notices.append(f'The server environment (EMAIL_GLOBAL_MODE={settings.EMAIL_GLOBAL_MODE}) limits sending to '
                           f'"{effective}", so the "{instance.sending_mode}" setting here has no effect yet.')
        elif effective == 'disabled':
            notices.append('Sending is disabled: queued emails wait and nothing is sent.')
        elif effective == 'redirect':
            notices.append(f'Redirect mode: every email goes to {instance.effective_redirect_to or "(no redirect address set)"}, '
                           'never to vendors.')
        if provider == 'console':
            notices.append('The console provider only prints messages on the server; nothing is delivered.')
        elif not provider_configured:
            notices.append(f'The {provider} provider is not fully configured on the server.')
        remaining = daily_remaining(timezone.now(), instance)
        data.update({
            'effective': {
                'mode': effective,
                'sender_name': instance.effective_sender_name,
                'sender_email': instance.effective_sender_email,
                'reply_to_email': instance.effective_reply_to,
                'redirect_to_email': instance.effective_redirect_to,
            },
            'environment': {
                'global_mode': settings.EMAIL_GLOBAL_MODE,
                'provider': provider,
                'provider_configured': provider_configured,
                'delivers_real_email': provider != 'console',
                'webhook_configured': bool(settings.ANYMAIL.get('RESEND_SIGNING_SECRET')) if provider == 'resend' else None,
                'daily_limit': settings.EMAIL_DAILY_LIMIT,
                'daily_remaining': remaining,
            },
            'live_sending_blocked': effective != 'enabled' or provider == 'console' or not provider_configured,
            'notices': notices,
        })
        return data


# --- Templates -----------------------------------------------------------------------------------

class EmailTemplateSerializer(serializers.ModelSerializer):
    updated_by = UserSummarySerializer(read_only=True)
    available_variables = serializers.SerializerMethodField()

    class Meta:
        model = EmailTemplate
        fields = [
            'key', 'name', 'description', 'category', 'mode', 'is_active', 'subject_template', 'html_template',
            'text_template', 'allowed_variables', 'available_variables', 'updated_by', 'updated_at',
        ]
        # Administrators edit the content and the mode; the rest is fixed by the code.
        read_only_fields = ['key', 'name', 'description', 'category', 'is_active', 'updated_by', 'updated_at']

    def get_available_variables(self, template):
        return VARIABLES

    def update(self, instance, validated_data):
        for name, value in validated_data.items():
            setattr(instance, name, value)
        instance.updated_by = self.context['request'].user
        try:
            instance.save()  # validates placeholders against the whitelist
        except DjangoValidationError as error:
            raise serializers.ValidationError(_django_errors(error)) from None
        return instance


class TemplatePreviewSerializer(serializers.Serializer):
    """Preview with a real application (optionally a booth line or weekend), or with sample data.
    Unsaved edits can be previewed by sending the template fields."""

    application = serializers.UUIDField(required=False, allow_null=True)
    application_line = serializers.PrimaryKeyRelatedField(queryset=ApplicationLine.objects.all(), required=False, allow_null=True)
    market_weekend = serializers.PrimaryKeyRelatedField(queryset=MarketWeekend.objects.all(), required=False, allow_null=True)
    subject_template = serializers.CharField(required=False, max_length=255)
    html_template = serializers.CharField(required=False)
    text_template = serializers.CharField(required=False)
    allowed_variables = serializers.ListField(child=serializers.CharField(), required=False)


# --- Messages ------------------------------------------------------------------------------------

class ReviewTaskSerializer(serializers.Serializer):
    def to_representation(self, task):
        return {
            'id': str(task.pk), 'status': task.status, 'is_open': task.is_open, 'title': task.title,
            'owner': UserSummarySerializer(task.owner).data if task.owner else None,
            'due_at': task.due_at, 'outcome': task.outcome,
        }


class EmailMessageSerializer(serializers.ModelSerializer):
    template_name = serializers.SerializerMethodField()
    status_label = serializers.CharField(source='get_status_display', read_only=True)
    vendor = serializers.SerializerMethodField()
    market_weekend = serializers.SerializerMethodField()
    application_line = serializers.SerializerMethodField()
    review_task = serializers.SerializerMethodField()
    created_by = UserSummarySerializer(read_only=True)
    permissions = serializers.SerializerMethodField()
    no_longer_applies = serializers.SerializerMethodField()

    class Meta:
        model = EmailMessage
        fields = [
            'id', 'template_key', 'template_name', 'status', 'status_label', 'subject', 'recipient_name',
            'recipient_email', 'delivered_to_email', 'redirected', 'application', 'vendor', 'market_weekend',
            'application_line', 'scheduled_for', 'sent_at', 'delivered_at', 'failure_code', 'failure_detail',
            'revision', 'review_task', 'created_by', 'created_at', 'updated_at', 'permissions', 'no_longer_applies',
        ]
        read_only_fields = fields

    def get_template_name(self, message):
        return message.template.name if message.template_id else 'One-off email'

    def get_vendor(self, message):
        return {'id': str(message.vendor_id), 'brand_name': message.vendor.brand_name} if message.vendor_id else None

    def get_market_weekend(self, message):
        weekend = message.market_weekend
        return {'id': str(weekend.pk), 'label': weekend.label, 'start_date': weekend.start_date} if weekend else None

    def get_application_line(self, message):
        line = message.application_line
        if line is None:
            return None
        return {'id': str(line.pk), 'product_name': line.product.name, 'weekend_label': line.product.weekend.label,
                'status': line.status}

    def get_review_task(self, message):
        return ReviewTaskSerializer(message.review_task).data if message.review_task_id else None

    def get_permissions(self, message):
        user = self.context['request'].user
        unresolved = message.status in services.UNRESOLVED_STATUSES
        return {
            'can_edit': services.can_edit(message, user),
            'can_approve': message.status == EmailMessage.Status.DRAFT and services.can_review(message, user),
            'can_cancel': unresolved and services.can_review(message, user),
        }

    def get_no_longer_applies(self, message):
        """For drafts and queued emails: why it would be stopped now (it will not be sent), else null."""
        if message.status not in services.UNRESOLVED_STATUSES:
            return None
        stop = stop_conditions.stop_reason(message)
        return stop[1] if stop else None


class EmailMessageDetailSerializer(EmailMessageSerializer):
    class Meta(EmailMessageSerializer.Meta):
        fields = [
            *EmailMessageSerializer.Meta.fields, 'html_body', 'text_body', 'sender_name', 'sender_email',
            'reply_to_email', 'provider', 'provider_message_id', 'attempt_count', 'event_key', 'trigger',
        ]
        read_only_fields = fields


class EmailRevisionSerializer(serializers.ModelSerializer):
    replaced_by = UserSummarySerializer(read_only=True)

    class Meta:
        model = EmailDraftRevision
        fields = ['number', 'subject', 'html_body', 'text_body', 'replaced_by', 'note', 'created_at']
        read_only_fields = fields


class ReviseDraftSerializer(serializers.Serializer):
    subject = serializers.CharField(required=False, allow_blank=True, max_length=255)
    html_body = serializers.CharField(required=False, allow_blank=True)
    text_body = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=300, default='')


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500, default='')


class PrepareDraftSerializer(serializers.Serializer):
    template_key = serializers.SlugField()
    application_line = serializers.PrimaryKeyRelatedField(queryset=ApplicationLine.objects.all(), required=False, allow_null=True)
    market_weekend = serializers.PrimaryKeyRelatedField(queryset=MarketWeekend.objects.all(), required=False, allow_null=True)
