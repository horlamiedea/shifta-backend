from decimal import Decimal

from django.db import transaction

from core.services import BaseService
from shifts.models import ShiftApplication
from . import ledger


class ShiftPayoutService(BaseService):
    """
    Pay a professional for a completed shift.

    Safe to call any number of times for the same application: clock-out
    schedules it, Release Funds can run it early, and the expiry job runs it
    for forgotten clock-outs. The idempotency key means only the first call
    moves money; later calls return the original transaction.
    """

    PAYABLE_STATUSES = ('CONFIRMED', 'COMPLETED', 'IN_PROGRESS')

    @transaction.atomic
    def __call__(self, application_id):
        application = (
            ShiftApplication.objects.select_for_update(of=('self',))
            .select_related('shift', 'professional__user')
            .filter(id=application_id).first()
        )
        if application is None:
            return None
        if application.status not in self.PAYABLE_STATUSES or not application.clock_out_time:
            return None  # Not eligible

        shift = application.shift
        # Hourly rate * scheduled duration
        duration = (shift.end_time - shift.start_time).total_seconds() / 3600
        amount = shift.rate * Decimal(str(duration))
        if amount < ledger.CENT:
            return None

        tx, _ = ledger.credit(
            application.professional.user, ledger.to_money(amount), 'PAYOUT',
            f"shift-payout:{application.id}", shift=shift,
            metadata={'reason': 'shift_completed', 'application_id': str(application.id)},
        )
        return tx


class ReleaseFundsService(BaseService):
    """Facility releases payment for a completed shift now, instead of after the 24h hold."""

    def __call__(self, user, application_id):
        if not user.is_facility:
            raise PermissionError("Only facilities can release funds.")

        if not ShiftApplication.objects.filter(
            id=application_id, shift__facility=user.facility,
        ).exists():
            raise PermissionError("Not your shift.")

        tx = ShiftPayoutService()(application_id)
        if tx is None:
            raise ValueError("This shift is not eligible for payment yet.")

        return {"status": "success", "message": "Funds released.", "amount": str(tx.amount)}
