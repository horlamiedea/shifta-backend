from django.db import transaction
from core.services import BaseService
from .models import Shift, ShiftApplication
from accounts.models import Review
from billing.models import Transaction
from decimal import Decimal
from django.utils import timezone
import uuid


def auto_promote_from_backlog(shift):
    """
    When a confirmed professional is removed, promote the next eligible
    PENDING applicant from the backlog (oldest first).
    Returns the promoted application or None.
    """
    if shift.quantity_filled >= shift.quantity_needed:
        return None  # Still full, no promotion needed

    # Reopen shift if it was FILLED
    if shift.status == 'FILLED':
        shift.status = 'OPEN'
        shift.save(update_fields=['status', 'updated_at'])

    # Find PENDING applicants ordered by application date (oldest = first in line)
    pending_apps = (
        ShiftApplication.objects
        .filter(shift=shift, status='PENDING')
        .select_related('professional__user')
        .order_by('created_at')
    )

    for candidate in pending_apps:
        # Check the candidate doesn't have a clashing CONFIRMED/IN_PROGRESS shift
        has_clash = ShiftApplication.objects.filter(
            professional=candidate.professional,
            status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'],
            shift__start_time__lt=shift.end_time,
            shift__end_time__gt=shift.start_time
        ).exists()

        if has_clash:
            continue  # Skip, try next candidate

        # Promote this candidate
        candidate.status = 'CONFIRMED'
        candidate.check_in_code = ShiftApplication.generate_code()
        candidate.check_out_code = ShiftApplication.generate_code()
        candidate.save()

        shift.quantity_filled += 1
        if shift.quantity_filled >= shift.quantity_needed:
            shift.status = 'FILLED'
        shift.save()

        # Notify the professional
        from core.models import Notification
        pro_name = (
            f"{candidate.professional.user.first_name} "
            f"{candidate.professional.user.last_name}".strip()
            or candidate.professional.user.email
        )
        Notification.objects.create(
            user=candidate.professional.user,
            title="Shift Confirmed — You're In!",
            message=(
                f"Great news! A spot opened up for '{shift.role}' at {shift.facility.name} "
                f"and you've been confirmed. Your check-in code is {candidate.check_in_code}."
            ),
            notification_type="SHIFT_APPROVED",
            related_object_id=candidate.id,
        )

        # Send email notification
        from .emails import send_shift_promotion_email
        send_shift_promotion_email(candidate)

        # Auto-reject this professional's other clashing PENDING apps
        clashing_pending = ShiftApplication.objects.filter(
            professional=candidate.professional,
            status='PENDING',
            shift__start_time__lt=shift.end_time,
            shift__end_time__gt=shift.start_time
        ).exclude(shift=shift).select_related('shift__facility__user')

        for clash_app in clashing_pending:
            clash_app.status = 'REJECTED'
            clash_app.save(update_fields=['status', 'updated_at'])

            Notification.objects.create(
                user=clash_app.shift.facility.user,
                title="Applicant No Longer Available",
                message=(
                    f"{pro_name} has been confirmed for another shift at the same time "
                    f"and is no longer available for '{clash_app.shift.role}'."
                ),
                notification_type="CANCELLED",
                related_object_id=clash_app.id,
            )

        return candidate

    return None  # No eligible candidate found


class FacilityCancelShiftService(BaseService):
    @transaction.atomic
    def __call__(self, user, shift_id, professional_id=None):
        if not user.is_facility:
            raise PermissionError("Only facilities can cancel shifts.")

        shift = Shift.objects.get(id=shift_id)
        if shift.facility != user.facility:
            raise PermissionError("Not your shift.")

        if professional_id:
            try:
                application = ShiftApplication.objects.get(
                    shift=shift, professional__id=professional_id, status='CONFIRMED'
                )
            except ShiftApplication.DoesNotExist:
                raise ValueError("No confirmed application for this professional.")

            if application.clock_in_time:
                raise ValueError("Cannot remove professional who has started the shift.")

            # Calculate Amounts
            duration = (shift.end_time - shift.start_time).total_seconds() / 3600
            total_cost = shift.rate * Decimal(duration)

            penalty_amount = total_cost * Decimal('0.10')  # 10%
            compensation_amount = total_cost * Decimal('0.03')  # 3%
            refund_amount = total_cost - penalty_amount  # 90%

            # Refund Facility
            shift.facility.wallet_balance += refund_amount
            shift.facility.save()

            # Credit Professional
            application.professional.wallet_balance += compensation_amount
            application.professional.save()

            # Log Transactions
            Transaction.objects.create(
                user=user,
                amount=refund_amount,
                transaction_type='REFUND',
                reference=str(uuid.uuid4()),
                status='SUCCESS',
                shift=shift
            )

            Transaction.objects.create(
                user=application.professional.user,
                amount=compensation_amount,
                transaction_type='PAYOUT',
                reference=str(uuid.uuid4()),
                status='SUCCESS',
                shift=shift
            )

            # Update Application
            application.status = 'CANCELLED'
            application.save()

            # Update Shift
            shift.quantity_filled -= 1
            shift.save()

            # Auto-promote next backlog candidate
            promoted = auto_promote_from_backlog(shift)
            msg = "Professional removed. Refund processed."
            if promoted:
                promo_name = (
                    f"{promoted.professional.user.first_name} "
                    f"{promoted.professional.user.last_name}".strip()
                    or promoted.professional.user.email
                )
                msg += f" {promo_name} has been auto-confirmed from the waitlist."

            return {"status": "success", "message": msg}

        else:
            pass


class ProfessionalCancelShiftService(BaseService):
    @transaction.atomic
    def __call__(self, user, shift_id):
        if not user.is_professional:
            raise PermissionError("Only professionals can cancel.")

        try:
            application = ShiftApplication.objects.get(
                shift__id=shift_id, professional=user.professional, status='CONFIRMED'
            )
        except ShiftApplication.DoesNotExist:
            raise ValueError("No confirmed application.")

        shift = application.shift
        now = timezone.now()

        # Check time for penalty (cutoff 4 hours before start)
        cutoff_time = shift.start_time - timezone.timedelta(hours=4)

        if now > cutoff_time:
            Review.objects.create(
                reviewer=shift.facility.user,
                target_user=user,
                rating=1,
                comment="Automatic review: Late cancellation."
            )
            message = "Cancelled with penalty."
        else:
            message = "Cancelled successfully."

        # Update Application
        application.status = 'CANCELLED'
        application.save()

        # Reopen Slot
        shift.quantity_filled -= 1
        shift.save()

        # Auto-promote next backlog candidate
        promoted = auto_promote_from_backlog(shift)
        if promoted:
            promo_name = (
                f"{promoted.professional.user.first_name} "
                f"{promoted.professional.user.last_name}".strip()
                or promoted.professional.user.email
            )
            # Notify facility about auto-promotion
            from core.models import Notification
            Notification.objects.create(
                user=shift.facility.user,
                title="Backlog Professional Auto-Confirmed",
                message=(
                    f"{promo_name} has been automatically confirmed for '{shift.role}' "
                    f"to replace the cancelled professional."
                ),
                notification_type="SHIFT_APPROVED",
                related_object_id=promoted.id,
            )

        return {"status": "success", "message": message}
