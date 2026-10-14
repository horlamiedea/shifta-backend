"""
Proves every naira in the app is accounted for.

Rule 1 — a balance is only ever the sum of its ledger entries. Any difference
("untracked") came from a direct edit or the seed command and has no trail.

Rule 2 — money only enters from outside through a confirmed payment:
  backed     FUNDING confirmed with Paystack                 key paystack:...
             bank inflows confirmed in Embedly's records      key embedly-inflow:...
             admin funding citing the payment received        key admin-fund:... + metadata.payment_reference
  legacy     FUNDING from before the ledger (no key). Old code only credited
             after Paystack said "success", so it is given the benefit of the
             doubt but listed separately; never written off automatically.
  unbacked   everything else that adds money from outside: adjustments, seed
             test money, admin top-ups citing no payment, untracked balance.

Internal entries move money between accounts and create none: CHARGE
(facility -> shift escrow), REFUND (escrow -> facility), PAYOUT (escrow ->
professional), WITHDRAWAL (professional -> bank), REVERSAL (failed withdrawal
back to the professional).

Per account, the balance it is entitled to is
    backed in + legacy in + refunds + payouts + reversals - charges - withdrawals
(never below zero); anything above that is unbacked "over" money.

A shift can only pay out what it was charged. Refunds and payouts beyond a
shift's charges (the old code paid seeded, never-charged shifts) are unbacked,
shared across their recipients in proportion; the ledger now refuses them
(billing.ledger.EscrowShortfall).

For the whole system, the cash Shifta should hold (real money in minus real
money paid out to banks) must cover every balance plus escrow on unfinished
work. A shortfall means unbacked money has already been spent.
"""
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from accounts.models import Facility, Professional
from . import ledger
from .models import Transaction

ZERO = Decimal('0.00')
CREDIT_TYPES = {'FUNDING', 'REFUND', 'PAYOUT', 'REVERSAL'}
BACKED_PREFIXES = ('paystack:', 'embedly-inflow:')


applied_entries = ledger.applied_entries


def inferred_expiry_refunds():
    """
    Refunds the old code paid without recording them: when a shift expired it
    refunded unfilled slots straight to the balance. Recomputed from the shift
    (rate x hours x unfilled) for completed shifts with no refund on record,
    but never more than the shift's escrow actually held: the old job also
    "refunded" shifts that were never charged (seeded shifts), and that money
    was never real. The ledger now records these as shift-unfilled-refund.
    Returns {facility user_id: [(shift, amount), ...]}.
    """
    from shifts.models import Shift

    refunds = {}
    shifts = (
        Shift.objects.filter(status='COMPLETED')
        .exclude(transaction__transaction_type='REFUND')
        .select_related('facility')
    )
    for shift in shifts:
        unfilled = shift.quantity_needed - shift.quantity_filled
        hours = (shift.end_time - shift.start_time).total_seconds() / 3600
        if unfilled > 0 and hours > 0:
            amount = ledger.to_money(shift.rate * Decimal(str(hours)) * unfilled)
            amount = min(amount, max(ledger.shift_escrow(shift.pk), ZERO))
            if amount > 0:
                refunds.setdefault(shift.facility.user_id, []).append((shift, amount))
    return refunds


def signed(tx):
    direction = tx.direction or ('CREDIT' if tx.transaction_type in CREDIT_TYPES else 'DEBIT')
    return tx.amount if direction == 'CREDIT' else -tx.amount


def source_of(tx):
    """'backed' | 'legacy' | 'unbacked' | 'internal' for one applied entry."""
    key = tx.idempotency_key or ''
    if tx.transaction_type == 'FUNDING':
        if key.startswith(BACKED_PREFIXES):
            return 'backed'
        if key.startswith('admin-fund:'):
            return 'backed' if (tx.metadata or {}).get('payment_reference') else 'unbacked'
        return 'legacy' if not key else 'unbacked'
    if tx.transaction_type == 'ADJUSTMENT':
        return 'unbacked'
    return 'internal'


@dataclass
class Statement:
    user_id: object
    email: str
    kind: str  # facility | professional
    balance: Decimal
    ledger_sum: Decimal = ZERO
    backed_in: Decimal = ZERO
    legacy_in: Decimal = ZERO
    unbacked_in: Decimal = ZERO  # net of unbacked credits and debits
    inferred_refunds: Decimal = ZERO  # old unrecorded expiry refunds, rebuilt from shifts
    internal_net: Decimal = ZERO
    entries: int = 0
    unbacked_detail: list = field(default_factory=list)

    @property
    def untracked(self):
        """Balance with no ledger entry. Negative means money is MISSING."""
        return self.balance - self.ledger_sum - self.inferred_refunds

    @property
    def entitled(self):
        return max(self.backed_in + self.legacy_in + self.internal_net + self.inferred_refunds, ZERO)

    @property
    def over(self):
        """Money in the balance that no real payment backs."""
        return max(self.balance - self.entitled, ZERO)


def overdrawn_shifts():
    """{shift_id: fraction of its payouts/refunds that its charges never covered}."""
    from django.db.models import Sum

    totals = {}
    rows = (
        applied_entries().filter(shift__isnull=False)
        .values('shift_id', 'transaction_type').annotate(total=Sum('amount'))
    )
    for row in rows:
        charged, out = totals.get(row['shift_id'], (ZERO, ZERO))
        if row['transaction_type'] == 'CHARGE':
            charged += row['total']
        elif row['transaction_type'] in ledger.ESCROW_OUTFLOWS:
            out += row['total']
        totals[row['shift_id']] = (charged, out)
    return {
        shift_id: (out - charged) / out
        for shift_id, (charged, out) in totals.items() if out > charged and out > 0
    }


def statements():
    """One Statement per facility and professional, from the ledger."""
    overdrawn = overdrawn_shifts()
    result = {}
    for model, kind in ((Facility, 'facility'), (Professional, 'professional')):
        for owner in model.objects.select_related('user'):
            result[owner.user_id] = Statement(owner.user_id, owner.user.email, kind, owner.wallet_balance)

    for tx in applied_entries().order_by('created_at'):
        st = result.get(tx.user_id)
        if st is None:
            continue
        amount = signed(tx)
        st.ledger_sum += amount
        st.entries += 1
        source = source_of(tx)
        fraction = overdrawn.get(tx.shift_id) if tx.transaction_type in ledger.ESCROW_OUTFLOWS else None
        if fraction:
            # Paid from a shift that was never charged enough: that part is unbacked.
            fake = ledger.to_money(amount * fraction) if amount * fraction >= ledger.CENT else ZERO
            st.unbacked_in += fake
            st.internal_net += amount - fake
            if fake:
                st.unbacked_detail.append((tx.reference, tx.transaction_type, fake))
            continue
        if source == 'backed':
            st.backed_in += amount
        elif source == 'legacy':
            st.legacy_in += amount
        elif source == 'unbacked':
            st.unbacked_in += amount
            st.unbacked_detail.append((tx.reference, tx.transaction_type, amount))
        else:
            st.internal_net += amount

    for user_id, items in inferred_expiry_refunds().items():
        if user_id in result:
            result[user_id].inferred_refunds += sum((amount for _, amount in items), ZERO)
    return list(result.values())


def system_position(all_statements=None):
    """
    Cash Shifta should hold vs what it owes.

    should_hold = real money in (backed + legacy funding) - real money paid out
                  (net amounts of withdrawals that weren't reversed)
    owes        = every balance + escrow on unfinished work (platform_cover)
    """
    from .reconciliation import platform_cover

    all_statements = all_statements if all_statements is not None else statements()
    money_in = sum((s.backed_in + s.legacy_in for s in all_statements), ZERO)

    paid_out = ZERO
    for tx in applied_entries().filter(transaction_type='WITHDRAWAL').exclude(status='FAILED'):
        net = (tx.metadata or {}).get('net_amount')
        paid_out += Decimal(str(net)) if net is not None else tx.amount

    cover = platform_cover()
    balances = sum((s.balance for s in all_statements), ZERO)
    owes = balances + cover['shift_escrow'] + cover['unpaid_completed_work']
    should_hold = money_in - paid_out
    return {
        'money_in': money_in,
        'paid_out_to_banks': paid_out,
        'should_hold': should_hold,
        'balances': balances,
        'shift_escrow': cover['shift_escrow'],
        'unpaid_completed_work': cover['unpaid_completed_work'],
        'owes': owes,
        'shortfall': max(owes - should_hold, ZERO),
    }


def write_off(statement, run_id=None):
    """
    Bring one account down to the money it is entitled to, with a full trail:
      1. old unrecorded expiry refunds are recorded as REFUND entries on their
         shifts, and any remaining untracked balance as an ADJUSTMENT — both
         without changing the balance — so the ledger explains it as it stands;
      2. the unbacked excess is debited as an ADJUSTMENT write-off.
    Missing money (balance below its ledger) is never touched here: it needs a
    human decision. Returns the amount written off.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    with transaction.atomic():
        owner = ledger.lock_wallet_owner(_user(statement))
        fresh = next(s for s in statements() if s.user_id == statement.user_id)
        if fresh.untracked < 0:
            raise ValueError(f"{fresh.email}: {-fresh.untracked} is missing from the balance; resolve manually.")
        for shift, amount in inferred_expiry_refunds().get(fresh.user_id, []):
            Transaction.objects.create(
                user_id=fresh.user_id, amount=amount, transaction_type='REFUND',
                direction=Transaction.CREDIT, balance_after=owner.wallet_balance, status='SUCCESS', shift=shift,
                reference=f"AUDIT-REF-{run_id}-{str(shift.pk)[:8]}",
                idempotency_key=f"legacy-expiry-refund:{shift.pk}",
                metadata={'reason': 'Unfilled-slot refund the old system paid without recording; recorded by audit'},
            )
        if fresh.untracked > 0:
            Transaction.objects.create(
                user_id=fresh.user_id, amount=fresh.untracked, transaction_type='ADJUSTMENT',
                direction=Transaction.CREDIT, balance_after=owner.wallet_balance, status='SUCCESS',
                reference=f"AUDIT-REC-{run_id}-{str(fresh.user_id)[:8]}",
                idempotency_key=f"audit-recognise:{fresh.user_id}:{run_id}",
                metadata={'reason': 'Balance with no ledger entry, recorded by audit', 'unbacked': True,
                          'recorded_at': timezone.now().isoformat()},
            )
        over = max(fresh.balance - fresh.entitled, ZERO)
        if over > 0:
            ledger.debit(
                _user(fresh), over, 'ADJUSTMENT', f"audit-writeoff:{fresh.user_id}:{run_id}",
                settle=False,
                metadata={'reason': 'Unbacked money written off by audit', 'entitled': str(fresh.entitled),
                          'balance_before': str(fresh.balance)},
            )
        return over


def _user(statement):
    from accounts.models import User
    return User.objects.get(pk=statement.user_id)
