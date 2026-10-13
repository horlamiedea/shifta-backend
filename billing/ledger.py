"""
App wallet ledger — the only code allowed to change a wallet_balance.

Every credit or debit:
  1. locks the owner's Facility/Professional row (SELECT ... FOR UPDATE), so
     concurrent requests for the same wallet run one after another;
  2. is keyed by an idempotency key that is unique in the database, so the
     same business event (a shift payout, a webhook, a retried task) can never
     be applied twice;
  3. writes a Transaction row recording the direction and resulting balance;
  4. for professionals, queues a WalletTransfer so their Embedly wallet makes
     the same movement against the platform wallet. The transfer is sent after
     the DB transaction commits.

Where the money is on Embedly:
  Facilities have no Embedly wallet. They fund through Paystack and their
  balances (and shift escrow) are held in Shifta's platform wallet, so facility
  movements are ledger-only; reconciliation checks the platform wallet covers
  them (billing.reconciliation.platform_cover).
  professional credit (pay, comp)       platform wallet  -> professional wallet
  professional debit (withdrawal)       handled by the withdrawal service
"""
import logging
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction

from accounts.models import Facility, Professional
from .models import Transaction, WalletTransfer

logger = logging.getLogger(__name__)

CENT = Decimal('0.01')


class LedgerError(Exception):
    pass


class InsufficientFunds(LedgerError, ValueError):
    """Subclasses ValueError so existing views keep answering 400."""


class IdempotencyConflict(LedgerError):
    """The key was already used for a different movement."""


def to_money(value):
    """Parse an amount into a positive Decimal with two decimal places."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid amount.")
    if not amount.is_finite():
        raise ValueError("Invalid amount.")
    amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    return amount


def lock_wallet_owner(user):
    """Return the user's Facility or Professional row, locked for this transaction."""
    for model in (Facility, Professional):
        owner = model.objects.select_for_update().filter(user_id=user.pk).first()
        if owner is not None:
            return owner
    raise LedgerError(f"User {user.pk} has no wallet.")


def credit(user, amount, transaction_type, idempotency_key, **kwargs):
    return post(user, amount, Transaction.CREDIT, transaction_type, idempotency_key, **kwargs)


def debit(user, amount, transaction_type, idempotency_key, **kwargs):
    return post(user, amount, Transaction.DEBIT, transaction_type, idempotency_key, **kwargs)


@transaction.atomic
def post(user, amount, direction, transaction_type, idempotency_key, *,
         shift=None, metadata=None, settle=True, narration='', reference=None,
         pending=None, status='SUCCESS'):
    """
    Apply one balance change. Returns (transaction, created).

    `created` is False when this idempotency key was already applied; the
    original Transaction is returned and nothing changes, so callers can skip
    follow-up side effects such as notifications.

    settle=False records the movement in the app only — use it when the money
    has already moved on Embedly (an inflow webhook) or when the caller queues
    its own transfer (withdrawals). Facility movements never settle: facility
    money lives in the platform wallet already.

    pending: an existing PENDING Transaction (e.g. a Paystack checkout) to
    complete in place instead of creating a second history row.

    status: the user-facing status of the history row. The balance changes
    either way; withdrawals pass PROCESSING until the bank confirms.
    """
    if not idempotency_key:
        raise LedgerError("An idempotency key is required.")
    amount = to_money(amount)

    owner = lock_wallet_owner(user)

    # Checked after taking the lock: every posting for this wallet holds the
    # same lock, so a concurrent duplicate is already committed and visible.
    existing = Transaction.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        if (existing.user_id, existing.amount, existing.direction) != (user.pk, amount, direction):
            raise IdempotencyConflict(
                f"Idempotency key {idempotency_key!r} was used for a different movement."
            )
        return existing, False

    if direction == Transaction.DEBIT:
        if owner.wallet_balance < amount:
            raise InsufficientFunds(
                f"Insufficient wallet balance. Required: ₦{amount:,.2f}, "
                f"available: ₦{owner.wallet_balance:,.2f}."
            )
        owner.wallet_balance -= amount
    else:
        owner.wallet_balance += amount
    owner.save(update_fields=['wallet_balance', 'updated_at'])

    fields = dict(
        user=user,
        amount=amount,
        transaction_type=transaction_type,
        direction=direction,
        balance_after=owner.wallet_balance,
        idempotency_key=idempotency_key,
        status=status,
        shift=shift,
        metadata=metadata or {},
    )
    if pending is not None:
        for name, value in fields.items():
            setattr(pending, name, value)
        pending.save()
        tx = pending
    else:
        tx = Transaction.objects.create(reference=reference or uuid.uuid4().hex, **fields)

    if settle and isinstance(owner, Professional):
        is_debit = direction == Transaction.DEBIT
        queue_transfer(
            kind=WalletTransfer.WALLET_TO_WALLET,
            amount=amount,
            reference=f"SHF-{tx.reference}",
            source_user=user if is_debit else None,
            destination_user=None if is_debit else user,
            narration=narration or tx.get_transaction_type_display(),
            ledger_transaction=tx,
        )

    logger.info(
        "ledger %s %s %s user=%s balance_after=%s key=%s",
        direction, transaction_type, amount, user.pk, owner.wallet_balance, idempotency_key,
    )
    return tx, True


def queue_transfer(*, amount, reference, **fields):
    """
    Create WalletTransfer(s) and send them once the surrounding transaction commits.

    Wallet-to-wallet amounts above Embedly's single-debit cap are split into
    chunks with references `<reference>-1`, `-2`, ... Account numbers left
    blank are resolved from the users' wallets when sent, so a ledger entry
    never fails just because the Embedly wallet is still being provisioned.
    """
    amount = to_money(amount)
    cap = settings.EMBEDLY_MAX_TRANSFER_AMOUNT
    if fields.get('kind') == WalletTransfer.WALLET_TO_WALLET and cap and amount > cap:
        chunks = []
        remaining = amount
        while remaining > 0:
            chunks.append(min(cap, remaining))
            remaining -= chunks[-1]
        parts = [(f"{reference}-{i}", part) for i, part in enumerate(chunks, start=1)]
    else:
        parts = [(reference, amount)]

    transfers = [
        WalletTransfer.objects.create(reference=ref, amount=part, **fields)
        for ref, part in parts
    ]
    ids = [str(t.pk) for t in transfers]

    def _send():
        from .tasks import dispatch_transfer
        for transfer_id in ids:
            dispatch_transfer.delay(transfer_id)

    # robust: if the broker is down the commit still stands; the periodic
    # sweeper picks up anything left QUEUED.
    transaction.on_commit(_send, robust=True)
    return transfers
