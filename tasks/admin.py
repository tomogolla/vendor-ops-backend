from django.contrib import admin

from .models import Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ['title', 'task_type', 'status', 'priority', 'owner', 'due_at', 'attempt_count', 'outcome']
    list_filter = ['task_type', 'status', 'priority']
    search_fields = ['title', 'vendor__brand_name']
    autocomplete_fields = ['vendor', 'owner']
