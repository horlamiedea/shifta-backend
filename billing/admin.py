from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone

from .models import EmbedlyWallet, Invoice, PlatformWallet, Transaction, WalletTransfer, WebhookEvent


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('user', 'transaction_type', 'direction', 'amount', 'balance_after', 'status', 'reference', 'created_at')
    search_fields = ('user__email', 'reference', 'idempotency_key')
    list_filter = ('transaction_type', 'direction', 'status')
    # The ledger is append-only; corrections go through a new ADJUSTMENT entry.
    readonly_fields = [f.name for f in Transaction._meta.fields]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('facility', 'month', 'amount', 'status', 'created_at')
    search_fields = ('facility__name',)
    list_filter = ('status', 'month')


@admin.register(EmbedlyWallet)
class EmbedlyWalletAdmin(admin.ModelAdmin):
    list_display = ('user', 'status', 'account_number', 'provider_balance', 'expected_provider_balance',
                    'drift', 'last_reconciled_at')
    list_filter = ('status',)
    search_fields = ('user__email', 'account_number', 'customer_id')
    readonly_fields = ('provider_balance', 'expected_provider_balance', 'drift', 'last_reconciled_at')
    actions = ['retry_activation']

    @admin.action(description='Retry opening the Embedly wallet')
    def retry_activation(self, request, queryset):
        from .wallet_service import activate_wallet
        for wallet in queryset.filter(status=EmbedlyWallet.PENDING_TIER):
            activate_wallet(wallet.pk)
        self.message_user(request, 'Activation retried; see each wallet status.')


@admin.register(PlatformWallet)
class PlatformWalletAdmin(admin.ModelAdmin):
    """Managed with `manage.py setup_platform_wallet`."""
    list_display = ('account_number', 'kind', 'status', 'bank_name', 'updated_at')
    readonly_fields = [f.name for f in PlatformWallet._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WalletTransfer)
class WalletTransferAdmin(admin.ModelAdmin):
    list_display = ('reference', 'kind', 'amount', 'status', 'source_user', 'destination_user',
                    'attempts', 'last_error', 'created_at')
    list_filter = ('status', 'kind', 'is_opening_balance')
    search_fields = ('reference', 'provider_reference', 'source_account', 'destination_account')
    readonly_fields = [f.name for f in WalletTransfer._meta.fields]
    actions = ['retry_now', 'confirm_succeeded', 'confirm_failed']

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description='Retry now (queued / processing only)')
    def retry_now(self, request, queryset):
        from .tasks import dispatch_transfer
        ids = list(queryset.filter(status__in=(WalletTransfer.QUEUED, WalletTransfer.PROCESSING))
                   .values_list('id', flat=True))
        WalletTransfer.objects.filter(id__in=ids).update(next_attempt_at=None)
        for transfer_id in ids:
            dispatch_transfer.delay(str(transfer_id))
        self.message_user(request, f'{len(ids)} transfer(s) re-dispatched.')

    @admin.action(description='Needs review: I confirmed on Embedly it SUCCEEDED')
    def confirm_succeeded(self, request, queryset):
        from .transfers import _record
        for t in queryset.filter(status=WalletTransfer.NEEDS_REVIEW):
            _record(t, WalletTransfer.SUCCESS, error=f'Confirmed by {request.user.email}')
        self.message_user(request, 'Marked as succeeded.')

    @admin.action(description='Needs review: I confirmed on Embedly it did NOT happen')
    def confirm_failed(self, request, queryset):
        from .transfers import _record
        count = 0
        for t in queryset.filter(status=WalletTransfer.NEEDS_REVIEW):
            if t.kind == WalletTransfer.BANK_PAYOUT:
                # Reverses the withdrawal back to the professional.
                _record(t, WalletTransfer.FAILED, error=f'Confirmed not paid by {request.user.email}')
            else:
                # The app ledger already moved; requeue with a fresh reference.
                with transaction.atomic():
                    locked = WalletTransfer.objects.select_for_update().get(pk=t.pk)
                    locked.rejections += 1
                    locked.status = WalletTransfer.QUEUED
                    locked.next_attempt_at = timezone.now()
                    locked.last_error = f'Requeued by {request.user.email} after review'
                    locked.save(update_fields=['rejections', 'status', 'next_attempt_at', 'last_error', 'updated_at'])
            count += 1
        self.message_user(request, f'{count} transfer(s) resolved.', level=messages.WARNING)


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'status', 'event_key', 'attempts', 'error', 'created_at')
    list_filter = ('status', 'event_type')
    search_fields = ('event_key',)
    readonly_fields = [f.name for f in WebhookEvent._meta.fields]
