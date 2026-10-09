import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from .models import EmbedlyWallet, IdempotencyRecord, WalletTransfer, WebhookEvent

logger = logging.getLogger(__name__)


@shared_task
def payout_professional(application_id):
    """Pay a professional for a completed shift. Idempotent — see ShiftPayoutService."""
    from .services import ShiftPayoutService
    ShiftPayoutService()(application_id)


@shared_task
def dispatch_transfer(transfer_id):
    """Send one WalletTransfer to Embedly (queued by billing.ledger after commit)."""
    from .transfers import process_transfer
    process_transfer(transfer_id)


@shared_task
def sweep_wallet_transfers():
    """
    Pick up transfers that are due: queued while the broker was down, backing
    off after a rejection, or sent with an outcome still to confirm.
    """
    from .transfers import process_transfer

    due = WalletTransfer.objects.filter(
        status__in=(WalletTransfer.QUEUED, WalletTransfer.PROCESSING),
    ).filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now()))
    ids = list(due.order_by('created_at').values_list('id', flat=True)[:200])
    for transfer_id in ids:
        try:
            process_transfer(transfer_id)
        except Exception:
            logger.exception("Transfer %s failed during sweep", transfer_id)

    # Webhooks whose processing hit a transient error.
    from .webhooks import process_event
    retry_events = WebhookEvent.objects.filter(status=WebhookEvent.FAILED, attempts__lt=20)
    for event_id in retry_events.values_list('id', flat=True)[:100]:
        process_event(event_id)
    return len(ids)


@shared_task
def provision_missing_wallets():
    """Retry opening wallets for KYC'd customers Embedly hadn't approved/tiered yet."""
    from .platform import activate_platform_wallet
    from .wallet_service import activate_wallet

    try:
        activate_platform_wallet()
    except Exception:
        logger.exception("Platform wallet activation failed")

    waiting = EmbedlyWallet.objects.filter(status=EmbedlyWallet.PENDING_TIER).exclude(customer_id__isnull=True)
    activated = 0
    for wallet_id in waiting.values_list('id', flat=True)[:200]:
        try:
            if activate_wallet(wallet_id).status == EmbedlyWallet.ACTIVE:
                activated += 1
        except Exception:
            logger.exception("Wallet %s activation failed", wallet_id)
    return activated


@shared_task
def reconcile_wallets():
    from .reconciliation import reconcile_all
    results = reconcile_all()
    logger.info("Wallet reconciliation: %s", results)
    return results


@shared_task
def purge_idempotency_records():
    cutoff = timezone.now() - timedelta(days=2)
    deleted, _ = IdempotencyRecord.objects.filter(created_at__lt=cutoff).delete()
    return deleted
