"""Internal administration for the email foundation (Slice 1 has no Svelte screens).

Staff can look; only administrators (User.is_administrator) can change settings, templates and
suppressions. Message records are read-only history: the only changes are Approve and Cancel,
which go through the services.
"""

from django.contrib import admin, messages
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from accounts.permissions import is_admin

from . import services
from .models import EmailMessage, EmailRecipientPreference, EmailSettings, EmailTemplate
from .rendering import VARIABLES


class AdministratorsChangeMixin:
    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_staff)

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def has_change_permission(self, request, obj=None):
        return is_admin(request.user) and request.user.is_staff

    def has_add_permission(self, request):
        return self.has_change_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(EmailSettings)
class EmailSettingsAdmin(AdministratorsChangeMixin, admin.ModelAdmin):
    fieldsets = [
        ('Sending', {'fields': ['sending_mode', 'redirect_to_email', 'effective_values']}),
        ('Sender', {'fields': ['sender_name', 'sender_email', 'reply_to_email']}),
        ('Timing', {'fields': ['business_timezone', 'send_window_start', 'send_window_end']}),
        ('History', {'fields': ['updated_by', 'created_at', 'updated_at']}),
    ]
    readonly_fields = ['effective_values', 'updated_by', 'created_at', 'updated_at']

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not EmailSettings.objects.exists()

    @admin.display(description='In effect (environment applied)')
    def effective_values(self, obj):
        return format_html(
            'Mode: <strong>{}</strong> · From: {} &lt;{}&gt; · Reply-to: {} · Redirect to: {}',
            obj.effective_mode, obj.effective_sender_name, obj.effective_sender_email or '(not set)',
            obj.effective_reply_to or '(none)', obj.effective_redirect_to or '(not set)',
        )

    def changelist_view(self, request, extra_context=None):
        EmailSettings.load()  # the single row always exists
        return super().changelist_view(request, extra_context)


@admin.register(EmailTemplate)
class EmailTemplateAdmin(AdministratorsChangeMixin, admin.ModelAdmin):
    list_display = ['name', 'key', 'category', 'mode', 'is_active', 'updated_at']
    list_filter = ['category', 'mode', 'is_active']
    search_fields = ['name', 'key']
    readonly_fields = ['available_variables', 'updated_by', 'created_at', 'updated_at']
    fields = [
        'key', 'name', 'description', 'category', 'mode', 'is_active', 'allowed_variables', 'available_variables',
        'subject_template', 'html_template', 'text_template', 'updated_by', 'created_at', 'updated_at',
    ]

    def get_readonly_fields(self, request, obj=None):
        return [*self.readonly_fields, 'key'] if obj else self.readonly_fields

    @admin.display(description='Variables templates can use')
    def available_variables(self, obj):
        return format_html_join(mark_safe('<br>'), '{{{}}}: {}', VARIABLES.items())


@admin.register(EmailRecipientPreference)
class EmailRecipientPreferenceAdmin(AdministratorsChangeMixin, admin.ModelAdmin):
    list_display = ['email', 'vendor', 'suppressed', 'suppression_reason', 'marketing_consent', 'transactional_permitted', 'updated_at']
    list_filter = ['suppressed', 'suppression_reason', 'marketing_consent', 'transactional_permitted']
    search_fields = ['email', 'vendor__brand_name']
    autocomplete_fields = ['vendor']
    readonly_fields = ['updated_by', 'created_at', 'updated_at']

    def save_model(self, request, obj, form, change):
        obj.source = obj.source or 'admin'
        super().save_model(request, obj, form, change)


@admin.register(EmailMessage)
class EmailMessageAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'template_key', 'vendor', 'recipient_email', 'status', 'scheduled_for', 'sent_at', 'redirected']
    list_filter = ['status', 'template_key', 'redirected', 'provider']
    search_fields = ['recipient_email', 'vendor__brand_name', 'subject', 'event_key', 'provider_message_id']
    actions = ['approve_drafts', 'cancel_messages']
    readonly_fields = [field.name for field in EmailMessage._meta.fields]

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_staff)

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return super().get_actions(request) if is_admin(request.user) else {}

    def _run(self, request, queryset, operation, label):
        done = 0
        for message in queryset:
            try:
                operation(message, actor=request.user)
                done += 1
            except Exception as error:  # noqa: BLE001 - report each refusal and carry on
                self.message_user(request, f'{message}: {error}', messages.WARNING)
        if done:
            self.message_user(request, f'{label} {done} message(s).', messages.SUCCESS)

    @admin.action(description='Approve selected drafts (queue for the next send window)')
    def approve_drafts(self, request, queryset):
        self._run(request, queryset, services.approve_draft, 'Approved')

    @admin.action(description='Cancel selected drafts or queued messages')
    def cancel_messages(self, request, queryset):
        self._run(request, queryset, services.cancel_message, 'Cancelled')
