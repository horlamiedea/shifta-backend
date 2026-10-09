"""
Wallet reconciliation: does each Embedly wallet hold what the app says it should?

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


def reconcile_all():
    results = {'checked': 0, 'tallied': 0, 'drifted': 0, 'unreachable': 0}
    for wallet in EmbedlyWallet.objects.filter(status=EmbedlyWallet.ACTIVE).select_related('user'):
        drift = reconcile_wallet(wallet)
        results['checked'] += 1
        if drift is None:
            results['unreachable'] += 1
        elif drift == 0:
            results['tallied'] += 1
        else:
            results['drifted'] += 1
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
