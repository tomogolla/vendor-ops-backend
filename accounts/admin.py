from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    search_fields = ['username', 'email', 'first_name', 'last_name']
    list_display = ['username', 'email', 'first_name', 'last_name', 'role', 'is_active']
    list_filter = ['role', 'is_active', 'is_staff']
    fieldsets = BaseUserAdmin.fieldsets + (('Vendor Ops', {'fields': ('role',)}),)
    add_fieldsets = BaseUserAdmin.add_fieldsets + (('Vendor Ops', {'fields': ('role',)}),)
