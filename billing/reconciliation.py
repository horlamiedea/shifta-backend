"""
Wallet reconciliation: does each Embedly wallet hold what the app says it should?

Professionals: each Embedly wallet is checked against the app balance.
Facilities have no Embedly wallets, so platform_cover() instead checks that the
platform wallet holds everything owed to them and to professionals.

The app ledger moves instantly; Embedly catches up as queued transfers run.
So the balance Embedly *should* report right now is

    app balance + transfers still to leave the wallet - transfers still to arrive

and drift = Embedly's balance - that. Zero means the two tally. A short-lived
non-zero value can appear while a transfer is mid-flight; drift that is the
same on two consecutive runs is real and is logged as a warning.
"""
import logging
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from accounts.models import Facility, Professional
from . import ledger
from .embedly_client import embedly_client
from .models import EmbedlyWallet, WalletTransfer

logger = logging.getLogger(__name__)


def _owner_balance(user):
    """Read from the database: a cached user.facility may predate recent postings."""
    for model in (Facility, Professional):
        balance = model.objects.filter(user_id=user.pk).values_list('wallet_balance', flat=True).first()
        if balance is not None:
            return balance
    return Decimal('0')


def expected_provider_balance(user):
    open_transfers = WalletTransfer.objects.filter(status__in=WalletTransfer.OPEN_STATUSES)
    pending_out = open_transfers.filter(source_user=user).aggregate(s=Sum('amount'))['s'] or Decimal('0')
    pending_in = (
        open_transfers.filter(destination_user=user, kind=WalletTransfer.WALLET_TO_WALLET)
        .aggregate(s=Sum('amount'))['s'] or Decimal('0')
    )
    return _owner_balance(user) + pending_out - pending_in


def provider_balance(account_number):
    r = embedly_client.get_wallet_by_account(account_number)
    if not r.ok or not isinstance(r.data, dict):
        return None
    value = r.data.get('availableBalance')
    return None if value is None else Decimal(str(value)).quantize(Decimal('0.01'))


def reconcile_wallet(wallet):
    """Refresh one wallet's reconciliation fields. Returns drift, or None if Embedly was unreachable."""
    actual = provider_balance(wallet.account_number)
    if actual is None:
        return None
    expected = expected_provider_balance(wallet.user)
    drift = actual - expected

    if drift and wallet.drift == drift:
        logger.warning(
            "Wallet drift user=%s account=%s embedly=%s expected=%s drift=%s",
            wallet.user_id, wallet.account_number, actual, expected, drift,
        )
    EmbedlyWallet.objects.filter(pk=wallet.pk).update(
        provider_balance=actual, expected_provider_balance=expected, drift=drift,
        last_reconciled_at=timezone.now(),
    )
    return drift


def professional_wallets():
    """Only professionals have Embedly wallets; facility money is in the platform wallet."""
    return EmbedlyWallet.objects.filter(
        status=EmbedlyWallet.ACTIVE, user__professional__isnull=False,
    ).select_related('user')


def reconcile_all():
    results = {'checked': 0, 'tallied': 0, 'drifted': 0, 'unreachable': 0}
    for wallet in professional_wallets():
        drift = reconcile_wallet(wallet)
        results['checked'] += 1
        if drift is None:
            results['unreachable'] += 1
        elif drift == 0:
            results['tallied'] += 1
        else:
            results['drifted'] += 1

    cover = platform_cover()
    results['platform_surplus'] = cover['surplus']
    if cover['surplus'] is not None and cover['surplus'] < 0:
        logger.warning(
            "Platform wallet shortfall: holds %s but owes %s (facilities %s, escrow %s, unpaid work %s, "
            "queued to professionals %s). Top it up from the Paystack settlement account.",
            cover['platform_balance'], cover['required'], cover['facility_balances'],
            cover['shift_escrow'], cover['unpaid_completed_work'], cover['pending_to_professionals'],
        )
    return results


def queue_opening_balance(wallet):
    """
    Back an app balance that predates the user's Embedly wallet (e.g. seeded
    or pre-integration balances) with a one-off platform -> user transfer.
    At most once per user: the reference is fixed.
    """
    reference = f"SHF-OPEN-{wallet.user_id.hex}"
    if WalletTransfer.objects.filter(reference__startswith=reference).exists():
        return None, 'already queued'
    actual = provider_balance(wallet.account_number)
    if actual is None:
        return None, 'Embedly unreachable'
    shortfall = expected_provider_balance(wallet.user) - actual
    if shortfall <= 0:
        return None, 'nothing to back' if shortfall == 0 else f'Embedly holds {-shortfall} more than the app'
    transfers = ledger.queue_transfer(
        kind=WalletTransfer.WALLET_TO_WALLET, amount=shortfall, reference=reference,
        destination_user=wallet.user, narration='Opening balance', is_opening_balance=True,
    )
    return transfers, f'queued {shortfall}'


def platform_cover():
    """
    Does Shifta's platform wallet hold at least what Shifta owes?

    Facilities have no Embedly wallets: their money sits in the platform wallet.
    So instead of a per-facility tally, the platform wallet must cover
      - every facility's unspent balance,
      - escrow for shifts still open (charged, minus refunds and payouts so far),
      - completed work not yet paid (the 24h hold, or a stuck payout),
      - transfers already queued from the platform to professionals.
    Anything above that is Shifta's own money (fees, penalties). A negative
    `surplus` is a shortfall: the wallet needs topping up from the Paystack
    settlement account, or something has gone wrong.
    """
    from django.db.models import Exists, OuterRef
    from shifts.models import Shift, ShiftApplication
    from .models import Transaction
    from .platform import platform_account_number

    def total(qs):
        return qs.aggregate(s=Sum('amount'))['s'] or Decimal('0')

    facility_balances = Facility.objects.aggregate(s=Sum('wallet_balance'))['s'] or Decimal('0')

    active = Transaction.objects.filter(shift__status__in=('OPEN', 'FILLED'), status='SUCCESS')
    shift_escrow = max(
        total(active.filter(transaction_type='CHARGE'))
        - total(active.filter(transaction_type__in=('REFUND', 'PAYOUT'))),
        Decimal('0'),
    )

    paid = Transaction.objects.filter(
        transaction_type='PAYOUT', shift=OuterRef('shift'), user=OuterRef('professional__user'),
    )
    # Only what each shift's escrow can actually pay: the ledger refuses payouts
    # beyond it (never-charged seeded shifts owe nothing).
    owed_by_shift = {}
    for app in ShiftApplication.objects.filter(status='COMPLETED').exclude(Exists(paid)).select_related('shift'):
        hours = (app.shift.end_time - app.shift.start_time).total_seconds() / 3600
        if hours > 0:
            owed_by_shift[app.shift_id] = owed_by_shift.get(app.shift_id, Decimal('0')) + \
                ledger.to_money(app.shift.rate * Decimal(str(hours)))
    unpaid_work = sum(
        (min(owed, max(ledger.shift_escrow(shift_id), Decimal('0'))) for shift_id, owed in owed_by_shift.items()),
        Decimal('0'),
    )

    pending_to_professionals = total(WalletTransfer.objects.filter(
        status__in=WalletTransfer.OPEN_STATUSES, source_user__isnull=True, destination_user__isnull=False,
    ))

    required = facility_balances + shift_escrow + unpaid_work + pending_to_professionals
    account = platform_account_number()
    balance = provider_balance(account) if account else None
    return {
        'platform_account': account or None,
        'platform_balance': balance,
        'facility_balances': facility_balances,
        'shift_escrow': shift_escrow,
        'unpaid_completed_work': unpaid_work,
        'pending_to_professionals': pending_to_professionals,
        'required': required,
        'surplus': None if balance is None else balance - required,
    }
