from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


class AccountTypeFilter(admin.SimpleListFilter):
    """Team accounts by default; system accounts (e.g. "Website form") only when asked for."""

    title = 'account type'
    parameter_name = 'account'

    def lookups(self, request, model_admin):
        return [('system', 'System accounts'), ('all', 'All accounts')]

    def choices(self, changelist):
        yield {
            'selected': self.value() is None, 'display': 'Team accounts',
            'query_string': changelist.get_query_string(remove=[self.parameter_name]),
        }
        for value, label in self.lookup_choices:
            yield {
                'selected': self.value() == value, 'display': label,
                'query_string': changelist.get_query_string({self.parameter_name: value}),
            }

    def queryset(self, request, queryset):
        if self.value() == 'system':
            return queryset.filter(is_system=True)
        if self.value() == 'all':
            return queryset
        return queryset.filter(is_system=False)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    search_fields = ['username', 'email', 'first_name', 'last_name']
    list_display = ['username', 'email', 'first_name', 'last_name', 'role', 'is_active']
    list_filter = [AccountTypeFilter, 'role', 'is_active', 'is_staff']
    fieldsets = BaseUserAdmin.fieldsets + (('Vendor Ops', {'fields': ('role',)}),)
    add_fieldsets = BaseUserAdmin.add_fieldsets + (('Vendor Ops', {'fields': ('role',)}),)
