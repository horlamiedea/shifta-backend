"""
Embedly webhook processing.

The endpoint (views.EmbedlyWebhookView) verifies the signature, stores the
event once (deduplicated on `event_key`) and calls process_event(). Nothing in
the payload is trusted to move money:

  nip (inflow)  -> the credit is looked up in the wallet's Embedly history and
                   must match account, reference and amount before the app
                   wallet is credited (ledger key `embedly-inflow:<reference>`).
                   Transfers Shifta itself made are skipped: the ledger already
                   counted them.
  payout        -> the payout status is requeried before the withdrawal is
                   settled or reversed.
  customer.liveness.* -> resumes wallet opening after the KYC selfie; Embedly
                   refuses to open a wallet without a real tier anyway.

Embedly delivers at least once and retries non-2xx up to 5 times, so every
step is idempotent; processing failures are retried by the sweeper.
"""
import hashlib
import logging
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import ledger
from .embedly_client import embedly_client
from .models import EmbedlyWallet, WalletTransfer, WebhookEvent
from .platform import platform_account_number
from .transfers import FAILED_STATES, SUCCESS_STATES, settle_payout_from_webhook

logger = logging.getLogger(__name__)


class RetryLater(Exception):
    """Transient problem (Embedly unreachable, history not visible yet)."""


def _get(data, *names):
    """Embedly's docs show both camelCase and PascalCase keys; match either."""
    lowered = {str(k).lower(): v for k, v in (data or {}).items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ''):
            return value
    return None


def _decimal(value):
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        return None


def event_key_for(payload, raw_body):
    event = str(payload.get('event') or '').lower()
    data = payload.get('data') or {}
    reference = _get(data, 'reference', 'transactionReference', 'paymentReference', 'sessionId', 'submissionId')
    if reference:
        status = _get(data, 'status') or ''
        return f"{event}:{reference}:{status}".lower()[:191]
    return f"{event}:sha256:{hashlib.sha256(raw_body).hexdigest()}"


def record_event(payload, raw_body):
    """Store the event; returns (event, is_new). A redelivery returns the stored one."""
    key = event_key_for(payload, raw_body)
    try:
        with transaction.atomic():
            return WebhookEvent.objects.create(
                event_key=key, event_type=str(payload.get('event') or '')[:100], payload=payload,
            ), True
    except IntegrityError:
        return WebhookEvent.objects.get(event_key=key), False


def process_event(event_id):
    """Process a stored event if it still needs it. Idempotent."""
    with transaction.atomic():
        event = WebhookEvent.objects.select_for_update(skip_locked=True).filter(pk=event_id).first()
        if event is None or event.status in (WebhookEvent.PROCESSED, WebhookEvent.IGNORED):
            return event
        event.attempts += 1
        try:
            with transaction.atomic():
                handler = HANDLERS.get(event.event_type.lower())
                if handler is None:
                    outcome, note = WebhookEvent.IGNORED, f"Unhandled event type {event.event_type!r}"
                else:
                    outcome, note = handler(event.payload.get('data') or {})
        except RetryLater as exc:
            outcome, note = WebhookEvent.FAILED, f"retry: {exc}"
        except Exception as exc:  # recorded; the sweeper retries FAILED events
            logger.exception("Webhook %s failed", event.pk)
            outcome, note = WebhookEvent.FAILED, f"{exc.__class__.__name__}: {exc}"

        event.status = outcome
        event.error = note[:2000]
        if outcome != WebhookEvent.FAILED:
            event.processed_at = timezone.now()
        event.save(update_fields=['status', 'error', 'attempts', 'processed_at', 'updated_at'])
        return event


# -- inflow --------------------------------------------------------------------

def _our_accounts():
    accounts = set(
        EmbedlyWallet.objects.exclude(account_number__isnull=True).exclude(account_number='')
        .values_list('account_number', flat=True)
    )
    platform = platform_account_number()
    if platform:
        accounts.add(platform)
    return accounts


def _find_in_history(account_number, reference, amount, around):
    """Return the matching credit from Embedly's own records, or None."""
    start, end = around - timedelta(days=2), timezone.now() + timedelta(hours=1)
    for page in range(1, 6):
        r = embedly_client.get_account_history(account_number, start, end, page=page, page_size=100)
        if not r.ok:
            raise RetryLater(f"history lookup failed: {r.message}")
        data = r.data or {}
        for item in data.get('walletHistories') or []:
            refs = {str(item.get('transactionReference') or ''), str(item.get('transactionId') or '')}
            if (reference in refs and str(item.get('debitCreditIndicator', '')).upper() == 'C'
                    and _decimal(item.get('amount')) == amount):
                return item
        if page >= int(data.get('totalPages') or 1):
            break
    return None


def handle_inflow(data):
    account = str(_get(data, 'accountNumber') or '')
    reference = str(_get(data, 'reference', 'transactionReference') or '')
    amount = _decimal(_get(data, 'amount', 'transactionAmount'))
    sender = str(_get(data, 'senderAccountNumber') or '')
    if str(_get(data, 'dc') or 'C').upper() != 'C':
        return WebhookEvent.IGNORED, 'Not a credit'
    if not (account and reference and amount and amount > 0):
        return WebhookEvent.IGNORED, 'Missing account, reference or amount'

    # Our own wallet-to-wallet movements are already in the ledger.
    if WalletTransfer.objects.filter(reference=reference).exists() or reference.startswith('SHF-'):
        return WebhookEvent.IGNORED, 'Internal Shifta transfer'
    if sender and sender in _our_accounts():
        return WebhookEvent.IGNORED, 'Transfer between Shifta wallets'

    if account == platform_account_number():
        logger.info("Platform wallet inflow %s %s (no app wallet to credit)", reference, amount)
        return WebhookEvent.IGNORED, 'Inflow to platform wallet'

    wallet = EmbedlyWallet.objects.select_related('user').filter(account_number=account).first()
    if wallet is None:
        return WebhookEvent.IGNORED, f'No Shifta wallet for account {account}'

    when = parse_datetime(str(_get(data, 'dateOfTransaction') or '')) or timezone.now()
    if timezone.is_naive(when):
        when = timezone.make_aware(when, dt_timezone.utc)
    if _find_in_history(account, reference, amount, when) is None:
        # History can lag the webhook slightly; FAILED events are retried.
        raise RetryLater('credit not (yet) visible in Embedly history')

    tx, created = ledger.credit(
        wallet.user, amount, 'FUNDING', f"embedly-inflow:{reference}",
        settle=False, reference=f"EMB-{reference}"[:100],
        metadata={
            'channel': 'bank_transfer', 'sender_name': _get(data, 'senderName'),
            'sender_bank': _get(data, 'senderBank'), 'embedly_reference': reference,
        },
    )
    if created:
        _notify_funded(wallet.user, amount)
    return WebhookEvent.PROCESSED, f'Credited {amount} ({tx.reference})'


def _notify_funded(user, amount):
    from core.models import Notification
    transaction.on_commit(lambda: Notification.send(
        user=user, title='Wallet funded',
        message=f'₦{amount:,.2f} has been added to your Shifta wallet.',
        notification_type='BROADCAST', data={'type': 'wallet_funded', 'amount': str(amount)},
    ), robust=True)


# -- payout --------------------------------------------------------------------

def handle_payout(data):
    refs = {str(_get(data, k) or '') for k in (
        'transactionRef', 'transactionReference', 'paymentReference', 'customerTransactionReference', 'sessionId',
    )} - {''}
    candidates = WalletTransfer.objects.filter(
        kind=WalletTransfer.BANK_PAYOUT, status__in=WalletTransfer.OPEN_STATUSES,
    )
    transfer = candidates.filter(provider_reference__in=refs).first() if refs else None
    if transfer is None and refs:
        transfer = next((t for t in candidates.filter(reference__in=[r.split('-R')[0] for r in refs])), None)
    if transfer is None:
        # Fall back to the unique open payout from this wallet for this amount.
        amount = _decimal(_get(data, 'amount'))
        matches = list(candidates.filter(
            source_account=str(_get(data, 'debitAccountNumber') or ''), amount=amount,
        )[:2])
        transfer = matches[0] if len(matches) == 1 else None
    if transfer is None:
        return WebhookEvent.IGNORED, 'No open payout matches this webhook'

    # Confirm with Embedly before settling or reversing anything.
    if not transfer.provider_reference:
        raise RetryLater('payout has no Embedly reference yet')
    status = embedly_client.payout_status(transfer.provider_reference)
    state = str((status.data or {}).get('status', '')).upper() if status.ok else ''
    if state in SUCCESS_STATES:
        settle_payout_from_webhook(transfer, True, status.raw)
        return WebhookEvent.PROCESSED, f'Payout {transfer.reference} succeeded'
    if state in FAILED_STATES:
        settle_payout_from_webhook(transfer, False, status.raw, message=str(_get(data, 'deliveryStatusMessage') or state))
        return WebhookEvent.PROCESSED, f'Payout {transfer.reference} failed and was reversed'
    raise RetryLater(f'payout status not final yet ({state or status.message})')


# -- liveness (selfie KYC) -------------------------------------------------------

def _liveness_wallet(data):
    customer_id = str(_get(data, 'customerId') or '')
    return EmbedlyWallet.objects.filter(customer_id=customer_id).first() if customer_id else None


def handle_liveness_approved(data):
    from .wallet_service import activate_wallet

    wallet = _liveness_wallet(data)
    if wallet is None:
        return WebhookEvent.IGNORED, 'No Shifta wallet for this customer'
    if wallet.status == EmbedlyWallet.PENDING_LIVENESS:
        wallet.status = EmbedlyWallet.PENDING_TIER
        wallet.meta = {**wallet.meta, 'liveness': 'completed', 'liveness_url': '', 'last_error': ''}
        wallet.save(update_fields=['status', 'meta', 'updated_at'])
    # Embedly itself refuses to open a wallet without a tier, so this webhook
    # can't open one on a forged verdict.
    wallet = activate_wallet(wallet.pk)
    return WebhookEvent.PROCESSED, f'Liveness approved; wallet {wallet.status}'


def _liveness_not_approved(data, note):
    wallet = _liveness_wallet(data)
    if wallet is None:
        return WebhookEvent.IGNORED, 'No Shifta wallet for this customer'
    reason = (_get(data, 'failureReason') or {}).get('text') if isinstance(_get(data, 'failureReason'), dict) else None
    if wallet.status in (EmbedlyWallet.PENDING_LIVENESS, EmbedlyWallet.PENDING_TIER):
        wallet.status = EmbedlyWallet.PENDING_LIVENESS
        wallet.meta = {**wallet.meta, 'liveness': note, 'last_error': reason or note}
        wallet.save(update_fields=['status', 'meta', 'updated_at'])
    return WebhookEvent.PROCESSED, f'Liveness {note}'


HANDLERS = {
    'nip': handle_inflow,
    'payout': handle_payout,
    'customer.liveness.approved': handle_liveness_approved,
    # Declined: the user retries via POST /billing/wallet/kyc/ (a fresh selfie link).
    'customer.liveness.declined': lambda data: _liveness_not_approved(data, 'declined'),
    'customer.liveness.needs_review': lambda data: _liveness_not_approved(data, 'under_review'),
}
