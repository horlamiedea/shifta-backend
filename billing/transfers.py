"""
Executes queued WalletTransfers against Embedly.

No DB transaction is held open across an HTTP call:
  1. claim  - lock the row, check it is due, mark PROCESSING with a short lease
  2. call   - send to Embedly, or first requery if a previous attempt's
              outcome is unknown
  3. record - lock the row again and store the outcome

Outcomes:
  success                     -> SUCCESS
  unknown (timeout, 5xx)      -> stays PROCESSING; the sweeper requeries by
                                 reference and only resends if Embedly has no
                                 record of it — so money can't move twice
  rejected, may pass later    -> QUEUED with backoff and a fresh reference
    (insufficient funds, limit)  (nothing moved, so a new reference is safe)
  rejected otherwise          -> wallet-to-wallet: NEEDS_REVIEW
                                 bank payout: FAILED and the withdrawal is reversed
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .embedly_client import embedly_client
from .models import EmbedlyWallet, Transaction, WalletTransfer
from .platform import platform_account_number

logger = logging.getLogger(__name__)

LEASE = timedelta(minutes=5)
WALLET_WAIT = timedelta(minutes=15)
MAX_RETRYABLE_REJECTIONS = 8
PAYOUT_CONFIRMATION_WINDOW = timedelta(hours=48)
# Embedly codes meaning "no money moved, may succeed later".
RETRYABLE_REJECTION_CODES = {'-999', '51', '-2', '61', '65'}
RETRYABLE_REJECTION_PHRASES = ('insufficient', 'limit')

# Embedly already has this reference (verified on staging: code -18, and the
# money moved only once), so an earlier attempt we lost track of went through.
DUPLICATE_CODES = {'-18', '26', '94'}

SUCCESS_STATES = {'SUCCESS', 'SUCCESSFUL', 'COMPLETED', 'COMPLETE'}
FAILED_STATES = {'FAILED', 'FAILURE', 'REVERSED', 'DECLINED', 'REJECTED'}


def _backoff(attempt):
    return timedelta(minutes=min(5 * 2 ** max(attempt - 1, 0), 240))


def _wallet_account(user):
    if user is None:
        return platform_account_number() or None
    return (
        EmbedlyWallet.objects.filter(user=user, status=EmbedlyWallet.ACTIVE)
        .values_list('account_number', flat=True).first()
    )


def _is_retryable_rejection(response):
    message = (response.message or '').lower()
    return response.code in RETRYABLE_REJECTION_CODES or any(p in message for p in RETRYABLE_REJECTION_PHRASES)


def _is_duplicate(response):
    return response.code in DUPLICATE_CODES or 'duplicate' in (response.message or '').lower()


def _state(data):
    if isinstance(data, dict):
        return str(data.get('status', '')).upper()
    return ''


def process_transfer(transfer_id):
    """Advance one transfer as far as it can go right now. Safe to call concurrently."""
    now = timezone.now()

    # -- 1. claim ------------------------------------------------------------
    with transaction.atomic():
        t = (
            WalletTransfer.objects.select_for_update(skip_locked=True, of=('self',))
            .select_related('source_user', 'destination_user').filter(pk=transfer_id).first()
        )
        if t is None or t.status not in (WalletTransfer.QUEUED, WalletTransfer.PROCESSING):
            return t  # locked by another worker, or already final
        if t.next_attempt_at and t.next_attempt_at > now:
            return t  # backing off, or another worker holds the lease

        if not t.source_account:
            t.source_account = _wallet_account(t.source_user) or ''
        if t.kind == WalletTransfer.WALLET_TO_WALLET and not t.destination_account:
            t.destination_account = _wallet_account(t.destination_user) or ''
        if not (t.source_account and t.destination_account):
            t.next_attempt_at = now + WALLET_WAIT
            t.last_error = 'Waiting for wallet activation'
            t.save(update_fields=['source_account', 'destination_account', 'next_attempt_at',
                                  'last_error', 'updated_at'])
            return t

        outcome_unknown = t.status == WalletTransfer.PROCESSING
        t.status = WalletTransfer.PROCESSING
        t.attempts += 1
        t.next_attempt_at = now + LEASE
        t.save(update_fields=['source_account', 'destination_account', 'status', 'attempts',
                              'next_attempt_at', 'updated_at'])

    # -- 2 & 3. call and record ------------------------------------------------
    if t.kind == WalletTransfer.BANK_PAYOUT:
        return _advance_payout(t, outcome_unknown)
    return _advance_wallet_transfer(t, outcome_unknown)


def _advance_wallet_transfer(t, outcome_unknown):
    if outcome_unknown:
        status = embedly_client.wallet_to_wallet_status(t.sent_reference)
        if status.ok:
            state = _state(status.data)
            if state in SUCCESS_STATES:
                return _record(t, WalletTransfer.SUCCESS, provider_response=status.raw)
            if state in FAILED_STATES:
                return _rejected(t, status)
            return _record_pending(t, status.raw)
        if not status.not_found:
            return _record_pending(t, status.raw, error=status.message)
        # Embedly has no record of it: safe to send with the same reference.

    response = embedly_client.wallet_to_wallet(
        from_account=t.source_account, to_account=t.destination_account,
        amount=t.amount, reference=t.sent_reference, remarks=t.narration,
    )
    if response.ok:
        return _record(t, WalletTransfer.SUCCESS, provider_response=response.raw)
    if _is_duplicate(response):
        # A previous attempt landed after all; let its status decide.
        status = embedly_client.wallet_to_wallet_status(t.sent_reference)
        state = _state(status.data) if status.ok else ''
        if state in SUCCESS_STATES:
            return _record(t, WalletTransfer.SUCCESS, provider_response=status.raw)
        if state in FAILED_STATES:
            return _rejected(t, status)
        return _record_pending(t, status.raw, error='Duplicate reference; confirming status')
    if response.ambiguous:
        return _record_pending(t, response.raw, error=response.message or 'No response from Embedly')
    return _rejected(t, response)


def _advance_payout(t, outcome_unknown):
    if outcome_unknown:
        if not t.provider_reference:
            # The send may have reached Embedly but we never got its reference,
            # so there is nothing to requery and resending could pay twice.
            return _record(
                t, WalletTransfer.NEEDS_REVIEW,
                error='Payout outcome unknown and no Embedly reference to check; verify on the dashboard.',
            )
        status = embedly_client.payout_status(t.provider_reference)
        state = _state(status.data) if status.ok else ''
        if state in SUCCESS_STATES:
            return _record(t, WalletTransfer.SUCCESS, provider_response=status.raw)
        if state in FAILED_STATES:
            return _record(t, WalletTransfer.FAILED, provider_response=status.raw,
                           error=f"Bank payout failed: {status.message or state}")
        if timezone.now() - t.created_at > PAYOUT_CONFIRMATION_WINDOW:
            return _record(t, WalletTransfer.NEEDS_REVIEW, provider_response=status.raw,
                           error='Payout still unconfirmed after 48 hours.')
        return _record_pending(t, status.raw)

    owner = t.source_user
    source_name = (
        EmbedlyWallet.objects.filter(user=owner).values_list('account_name', flat=True).first()
        or f"{owner.first_name} {owner.last_name}".strip()
    )
    response = embedly_client.bank_payout(
        source_account=t.source_account, source_account_name=source_name,
        destination_account=t.destination_account, destination_bank_code=t.destination_bank_code,
        destination_account_name=t.destination_account_name, amount=t.amount,
        reference=t.sent_reference, remarks=t.narration,
    )
    if response.ok:
        data = response.data
        ref = data if isinstance(data, str) else (data or {}).get('transactionRef') if isinstance(data, dict) else None
        if ref:
            # Accepted; the bank leg is asynchronous. Poll until webhook/requery says final.
            return _record_pending(t, response.raw, provider_reference=ref, wait=timedelta(minutes=10))
        # Accepted but nothing to track it by: never treat as failed (it may pay out).
        return _record(t, WalletTransfer.NEEDS_REVIEW, provider_response=response.raw,
                       error='Payout accepted without an Embedly reference; verify on the dashboard.')
    if _is_duplicate(response):
        # Already submitted; Embedly tracks payouts by the reference we sent.
        return _record_pending(t, response.raw, provider_reference=t.sent_reference,
                               error='Duplicate reference; confirming status')
    if response.ambiguous:
        return _record_pending(t, response.raw, error=response.message or 'No response from Embedly')
    if _is_retryable_rejection(response):
        return _rejected(t, response)
    return _record(t, WalletTransfer.FAILED, provider_response=response.raw,
                   error=f"Bank payout rejected: {response.message}")


def _rejected(t, response):
    """Embedly said no and nothing moved."""
    error = response.message or f"Rejected (code {response.code})"
    if _is_retryable_rejection(response) and t.rejections + 1 < MAX_RETRYABLE_REJECTIONS:
        with transaction.atomic():
            locked = WalletTransfer.objects.select_for_update().get(pk=t.pk)
            locked.rejections += 1  # next send uses a fresh reference
            locked.status = WalletTransfer.QUEUED
            locked.last_error = error
            locked.provider_response = response.raw
            locked.next_attempt_at = timezone.now() + _backoff(locked.rejections)
            locked.save(update_fields=['rejections', 'status', 'last_error', 'provider_response',
                                       'next_attempt_at', 'updated_at'])
            return locked
    final = WalletTransfer.FAILED if t.kind == WalletTransfer.BANK_PAYOUT else WalletTransfer.NEEDS_REVIEW
    return _record(t, final, provider_response=response.raw, error=error)


def _record_pending(t, raw, *, error='', provider_reference=None, wait=None):
    with transaction.atomic():
        locked = WalletTransfer.objects.select_for_update().get(pk=t.pk)
        if locked.status != WalletTransfer.PROCESSING:
            return locked
        locked.provider_response = raw or {}
        locked.last_error = error
        if provider_reference:
            locked.provider_reference = provider_reference
        locked.next_attempt_at = timezone.now() + (wait or _backoff(locked.attempts))
        locked.save(update_fields=['provider_response', 'last_error', 'provider_reference',
                                   'next_attempt_at', 'updated_at'])
        return locked


def _record(t, status, *, provider_response=None, error=''):
    from .wallet_service import reverse_failed_withdrawal

    with transaction.atomic():
        locked = WalletTransfer.objects.select_for_update().get(pk=t.pk)
        if locked.status in (WalletTransfer.SUCCESS, WalletTransfer.FAILED):
            return locked  # a webhook got here first
        locked.status = status
        locked.last_error = error
        if provider_response is not None:
            locked.provider_response = provider_response
        locked.next_attempt_at = None
        if status in (WalletTransfer.SUCCESS, WalletTransfer.FAILED):
            locked.completed_at = timezone.now()
        locked.save(update_fields=['status', 'last_error', 'provider_response', 'next_attempt_at',
                                   'completed_at', 'updated_at'])

        if locked.kind == WalletTransfer.BANK_PAYOUT and locked.ledger_transaction_id:
            if status == WalletTransfer.SUCCESS:
                Transaction.objects.filter(pk=locked.ledger_transaction_id, status='PROCESSING') \
                    .update(status='SUCCESS', updated_at=timezone.now())
            elif status == WalletTransfer.FAILED:
                reverse_failed_withdrawal(locked)

    log = logger.warning if status in (WalletTransfer.FAILED, WalletTransfer.NEEDS_REVIEW) else logger.info
    log("transfer %s %s -> %s %s", locked.reference, locked.amount, status, error)
    return locked


def settle_payout_from_webhook(transfer, succeeded, raw, message=''):
    """Apply a payout webhook outcome (already confirmed by requery)."""
    if succeeded:
        return _record(transfer, WalletTransfer.SUCCESS, provider_response=raw)
    return _record(transfer, WalletTransfer.FAILED, provider_response=raw,
                   error=f"Bank payout failed: {message}")
