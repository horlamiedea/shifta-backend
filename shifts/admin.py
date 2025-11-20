from django.contrib import admin
from .models import Shift, ShiftApplication

@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ('role', 'facility', 'start_time', 'status', 'quantity_needed', 'quantity_filled', 'created_at')
    search_fields = ('role', 'facility__name')
    list_filter = ('status', 'start_time')
    raw_id_fields = ('facility',)

@admin.register(ShiftApplication)
class ShiftApplicationAdmin(admin.ModelAdmin):
    list_display = ('professional', 'shift', 'status', 'clock_in_time', 'clock_out_time', 'created_at')
    search_fields = ('professional__user__email', 'shift__role')
    list_filter = ('status',)
    raw_id_fields = ('professional', 'shift')
