from django.contrib import admin

from .models import ActivityEvent, Application, ApplicationLine, InternalNote, LeadIntakeEvent, Vendor


class IntakeInline(admin.TabularInline):
    model = LeadIntakeEvent
    extra = 0
    readonly_fields = ['source_type', 'source_detail', 'import_batch', 'captured_by', 'captured_at', 'metadata']
    can_delete = False


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ['brand_name', 'first_name', 'last_name', 'email', 'journey_stage', 'qualification_status',
                    'application_status_summary', 'owner', 'is_active', 'created_at']
    list_filter = ['journey_stage', 'qualification_status', 'application_status_summary', 'is_active', 'lead_source']
    search_fields = ['brand_name', 'first_name', 'last_name', 'email', 'instagram', 'phone']
    readonly_fields = ['email_normalized', 'phone_normalized', 'instagram_normalized', 'created_at', 'updated_at', 'created_by']
    autocomplete_fields = ['owner']
    inlines = [IntakeInline]


@admin.register(LeadIntakeEvent)
class LeadIntakeEventAdmin(admin.ModelAdmin):
    list_display = ['vendor', 'source_type', 'source_detail', 'captured_by', 'captured_at']
    list_filter = ['source_type']
    search_fields = ['vendor__brand_name']


class ApplicationLineInline(admin.TabularInline):
    """Read-only: line changes go through vendors.pipeline so capacity and activity stay correct."""
    model = ApplicationLine
    fk_name = 'application'
    extra = 0
    can_delete = False
    fields = ['variant', 'add_on', 'parent_line', 'quantity', 'unit_price_cents', 'status', 'status_reason',
              'waitlisted_at', 'confirmed_at', 'closed_at']
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    inlines = [ApplicationLineInline]
    list_display = ['vendor', 'stage', 'stage_entered_at', 'reviewer', 'source', 'submitted_at', 'billed_at']
    list_filter = ['stage', 'source']
    search_fields = ['vendor__brand_name']
    autocomplete_fields = ['vendor', 'reviewer']


@admin.register(InternalNote)
class InternalNoteAdmin(admin.ModelAdmin):
    list_display = ['vendor', 'note_type', 'author', 'created_at']
    list_filter = ['note_type']
    search_fields = ['vendor__brand_name', 'body']


@admin.register(ActivityEvent)
class ActivityEventAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'vendor', 'event_type', 'description', 'actor']
    list_filter = ['event_type']
    search_fields = ['vendor__brand_name', 'description']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
