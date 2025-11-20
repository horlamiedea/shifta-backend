from django.contrib import admin
from .models import Transaction, Invoice

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'transaction_type', 'amount', 'status', 'reference', 'created_at')
    search_fields = ('user__email', 'reference')
    list_filter = ('transaction_type', 'status')

@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('facility', 'month', 'amount', 'status', 'created_at')
    search_fields = ('facility__name',)
    list_filter = ('status', 'month')
