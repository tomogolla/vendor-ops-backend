from django.contrib import admin

from .models import ImportBatch


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ['file_name', 'created_by', 'created_at']
    readonly_fields = ['file_name', 'created_by', 'created_at', 'summary']
