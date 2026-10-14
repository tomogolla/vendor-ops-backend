from django.contrib import admin

from .models import AddOn, BoothVariant, MarketWeekend


class BoothVariantInline(admin.TabularInline):
    model = BoothVariant
    extra = 0


class AddOnInline(admin.TabularInline):
    model = AddOn
    extra = 0


@admin.register(MarketWeekend)
class MarketWeekendAdmin(admin.ModelAdmin):
    list_display = ['label', 'start_date', 'end_date', 'venue', 'status']
    list_filter = ['status']
    search_fields = ['label', 'venue']
    inlines = [BoothVariantInline, AddOnInline]
