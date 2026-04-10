"""
Cancellation Services

Tiered penalty system:

PROFESSIONAL cancels:
  > 4 h before shift  →  No financial penalty, tracked in stats
  1–4 h before shift  →  Auto 2★ rating, tracked
  < 1 h before shift  →  Auto 1★ rating, tracked, facility notified urgently

FACILITY removes a confirmed professional:
  > 4 h before shift  →  90 % refund to facility, 3 % comp to professional
  2–4 h before shift  →  80 % refund, 15 % comp to professional
  < 2 h before shift  →  60 % refund, 40 % comp to professional
"""

from django.db import transaction
from core.services import BaseService
from core.models import Notification
from .models import Shift, ShiftApplication
from .rating_service import RatingService, refresh_professional_stats
from billing.models import Transaction
from decimal import Decimal
from django.utils import timezone
import uuid


# ---------------------------------------------------------------------------
# Backlog auto-promotion (unchanged from previous implementation)
# ---------------------------------------------------------------------------

def auto_promote_from_backlog(shift):
    """
    When a confirmed professional is removed, promote the next eligible
    PENDING applicant from the backlog (oldest first, highest score tiebreaker).
    """
    if shift.quantity_filled >= shift.quantity_needed:
        return None

    if shift.status == 'FILLED':
        shift.status = 'OPEN'
        shift.save(update_fields=['status', 'updated_at'])

    pending_apps = (
        ShiftApplication.objects
        .filter(shift=shift, status='PENDING')
        .select_related('professional__user')
        .order_by('created_at')
    )

    for candidate in pending_apps:
        has_clash = ShiftApplication.objects.filter(
            professional=candidate.professional,
            status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'],
            shift__start_time__lt=shift.end_time,
            shift__end_time__gt=shift.start_time,
        ).exists()
        if has_clash:
            continue

        # Promote
        candidate.status = 'CONFIRMED'
        candidate.check_in_code = ShiftApplication.generate_code()
        candidate.check_out_code = ShiftApplication.generate_code()
        candidate.save()

        shift.quantity_filled += 1
        if shift.quantity_filled >= shift.quantity_needed:
            shift.status = 'FILLED'
        shift.save()

        pro_name = _pro_display_name(candidate.professional)

        Notification.send(
            user=candidate.professional.user,
            title="Shift Confirmed — You're In!",
            message=(
                f"Great news! A spot opened up for '{shift.role}' at {shift.facility.name} "
                f"and you've been confirmed. Your check-in code is {candidate.check_in_code}."
            ),
            notification_type="SHIFT_APPROVED",
            related_object_id=candidate.id,
        )

        from .emails import send_shift_promotion_email
        send_shift_promotion_email(candidate)

        # Auto-reject clashing PENDING apps at other shifts
        clashing = ShiftApplication.objects.filter(
            professional=candidate.professional,
            status='PENDING',
            shift__start_time__lt=shift.end_time,
            shift__end_time__gt=shift.start_time,
        ).exclude(shift=shift).select_related('shift__facility__user')

        for clash_app in clashing:
            clash_app.status = 'REJECTED'
            clash_app.save(update_fields=['status', 'updated_at'])
            Notification.send(
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

    return None


# ---------------------------------------------------------------------------
# Facility cancels / removes a confirmed professional
# ---------------------------------------------------------------------------

class FacilityCancelShiftService(BaseService):
    """
    Tiered compensation based on how close to shift start:
      > 4 h:  90 % refund, 3 % comp
      2–4 h:  80 % refund, 15 % comp
      < 2 h:  60 % refund, 40 % comp
    """

    @transaction.atomic
    def __call__(self, user, shift_id, professional_id=None, reason=''):
        if not user.is_facility:
            raise PermissionError("Only facilities can cancel shifts.")

        shift = Shift.objects.get(id=shift_id)
        if shift.facility != user.facility:
            raise PermissionError("Not your shift.")

        if not professional_id:
            raise ValueError("professional_id is required to remove a professional.")

        try:
            application = ShiftApplication.objects.select_related(
                'professional__user'
            ).get(shift=shift, professional__id=professional_id, status='CONFIRMED')
        except ShiftApplication.DoesNotExist:
            raise ValueError("No confirmed application for this professional.")

        if application.clock_in_time:
            raise ValueError("Cannot remove a professional who has already started the shift.")

        now = timezone.now()
        hours_until_start = (shift.start_time - now).total_seconds() / 3600
        duration_hours = (shift.end_time - shift.start_time).total_seconds() / 3600
        total_cost = shift.rate * Decimal(str(duration_hours))

        # Tiered penalty
        if hours_until_start > 4:
            refund_pct, comp_pct = Decimal('0.90'), Decimal('0.03')
            tier_label = "standard"
        elif hours_until_start > 2:
            refund_pct, comp_pct = Decimal('0.80'), Decimal('0.15')
            tier_label = "short-notice"
        else:
            refund_pct, comp_pct = Decimal('0.60'), Decimal('0.40')
            tier_label = "last-minute"

        refund_amount = total_cost * refund_pct
        compensation = total_cost * comp_pct

        # Wallet adjustments
        shift.facility.wallet_balance += refund_amount
        shift.facility.save(update_fields=['wallet_balance'])

        application.professional.wallet_balance += compensation
        application.professional.save(update_fields=['wallet_balance'])

        # Transaction logs
        Transaction.objects.create(
            user=user, amount=refund_amount,
            transaction_type='REFUND', reference=str(uuid.uuid4()),
            status='SUCCESS', shift=shift,
        )
        Transaction.objects.create(
            user=application.professional.user, amount=compensation,
            transaction_type='PAYOUT', reference=str(uuid.uuid4()),
            status='SUCCESS', shift=shift,
        )

        # Update application
        application.status = 'CANCELLED'
        application.cancelled_by = 'FACILITY'
        application.cancellation_reason = reason or f'Removed by facility ({tier_label})'
        application.save()

        # Update shift
        shift.quantity_filled -= 1
        shift.save()

        # Notify the professional
        pro_name = _pro_display_name(application.professional)
        Notification.send(
            user=application.professional.user,
            title="Shift Cancelled by Facility",
            message=(
                f"Your confirmed shift '{shift.role}' at {shift.facility.name} has been "
                f"cancelled by the facility. You have been compensated ₦{compensation:,.2f}."
            ),
            notification_type="CANCELLED",
            related_object_id=application.id,
        )

        # Auto-promote from backlog
        promoted = auto_promote_from_backlog(shift)
        msg = (
            f"Professional removed ({tier_label}). "
            f"Refund: ₦{refund_amount:,.2f}, Compensation: ₦{compensation:,.2f}."
        )
        if promoted:
            msg += f" {_pro_display_name(promoted.professional)} auto-confirmed from waitlist."

        return {"status": "success", "message": msg}


# ---------------------------------------------------------------------------
# Professional cancels their own confirmed shift
# ---------------------------------------------------------------------------

class ProfessionalCancelShiftService(BaseService):
    """
    Tiered penalties:
      > 4 h:  No penalty, just tracked
      1–4 h:  Auto 2★ rating
      < 1 h:  Auto 1★ rating (urgent)
    Reason is always required.
    """

    @transaction.atomic
    def __call__(self, user, shift_id, reason=''):
        if not user.is_professional:
            raise PermissionError("Only professionals can cancel.")

        try:
            application = ShiftApplication.objects.select_related(
                'shift__facility__user', 'professional'
            ).get(
                shift__id=shift_id, professional=user.professional, status='CONFIRMED'
            )
        except ShiftApplication.DoesNotExist:
            raise ValueError("No confirmed application for this shift.")

        if not reason.strip():
            raise ValueError("Please provide a reason for cancellation.")

        shift = application.shift
        now = timezone.now()
        hours_until_start = (shift.start_time - now).total_seconds() / 3600

        # Determine penalty tier
        if hours_until_start > 4:
            auto_rating = None
            tier_label = "standard"
            message = "Shift cancelled successfully. This cancellation has been recorded."
        elif hours_until_start > 1:
            auto_rating = 2
            tier_label = "short-notice"
            message = (
                "Shift cancelled. Because this was less than 4 hours before the shift, "
                "a 2-star rating has been recorded."
            )
        else:
            auto_rating = 1
            tier_label = "last-minute"
            message = (
                "Shift cancelled. Because this was less than 1 hour before the shift, "
                "a 1-star rating has been recorded."
            )

        # Update application
        application.status = 'CANCELLED'
        application.cancelled_by = 'PROFESSIONAL'
        application.cancellation_reason = reason
        application.save()

        # Reopen slot
        shift.quantity_filled -= 1
        shift.save()

        # Auto-rating penalty
        if auto_rating is not None:
            RatingService.create_auto_review(
                application=application,
                rating=auto_rating,
                comment=f"Auto-review: {tier_label} cancellation ({reason})",
                reviewer_user=shift.facility.user,
            )
        else:
            # Still refresh stats for cancellation tracking
            refresh_professional_stats(application.professional)

        # Notify facility
        pro_name = _pro_display_name(application.professional)
        urgency = " ⚠️ URGENT —" if tier_label == "last-minute" else ""
        Notification.send(
            user=shift.facility.user,
            title=f"{urgency} Professional Cancelled Shift",
            message=(
                f"{pro_name} has cancelled '{shift.role}' ({tier_label}). "
                f"Reason: {reason}"
            ),
            notification_type="CANCELLED",
            related_object_id=application.id,
            data={
                "tier": tier_label,
                "hours_until_start": round(hours_until_start, 1),
                "reason": reason,
            },
        )

        # Auto-promote from backlog
        promoted = auto_promote_from_backlog(shift)
        if promoted:
            promo_name = _pro_display_name(promoted.professional)
            Notification.send(
                user=shift.facility.user,
                title="Backlog Professional Auto-Confirmed",
                message=(
                    f"{promo_name} has been automatically confirmed for '{shift.role}' "
                    f"to replace the cancelled professional."
                ),
                notification_type="SHIFT_APPROVED",
                related_object_id=promoted.id,
            )
            message += f" {promo_name} has been auto-confirmed from the waitlist."

        return {"status": "success", "message": message}


# ---------------------------------------------------------------------------
# Facility deletes an entire shift (only before it has started)
# ---------------------------------------------------------------------------

class FacilityDeleteShiftService(BaseService):
    """
    Delete a shift entirely. Only allowed if the shift has not started yet.
    - All PENDING applications are rejected
    - All CONFIRMED applications are cancelled with full compensation
    - Remaining budget (unfilled slots) refunded to facility
    """

    @transaction.atomic
    def __call__(self, user, shift_id, reason=''):
        if not user.is_facility:
            raise PermissionError("Only facilities can delete shifts.")

        shift = Shift.objects.select_for_update().get(id=shift_id)
        if shift.facility != user.facility:
            raise PermissionError("Not your shift.")

        if shift.status in ('COMPLETED', 'CANCELLED'):
            raise ValueError("This shift is already completed or cancelled.")

        now = timezone.now()
        if shift.start_time <= now:
            raise ValueError("Cannot delete a shift that has already started. Use 'End Shift' instead.")

        # Check for any IN_PROGRESS applications
        if ShiftApplication.objects.filter(shift=shift, status='IN_PROGRESS').exists():
            raise ValueError("Cannot delete a shift with active professionals. Use 'End Shift' instead.")

        duration_hours = (shift.end_time - shift.start_time).total_seconds() / 3600
        cost_per_slot = shift.rate * Decimal(str(duration_hours))

        # Cancel all CONFIRMED applications — full compensation since facility chose to delete
        confirmed_apps = ShiftApplication.objects.filter(
            shift=shift, status='CONFIRMED'
        ).select_related('professional__user')

        for app in confirmed_apps:
            compensation = cost_per_slot * Decimal('0.40')  # 40% comp for deletion

            app.professional.wallet_balance += compensation
            app.professional.save(update_fields=['wallet_balance'])

            Transaction.objects.create(
                user=app.professional.user, amount=compensation,
                transaction_type='PAYOUT', reference=str(uuid.uuid4()),
                status='SUCCESS', shift=shift,
            )

            app.status = 'CANCELLED'
            app.cancelled_by = 'FACILITY'
            app.cancellation_reason = reason or 'Shift deleted by facility'
            app.save()

            Notification.send(
                user=app.professional.user,
                title="Shift Deleted by Facility",
                message=(
                    f"The shift '{shift.role}' at {shift.facility.name} has been deleted. "
                    f"You have been compensated ₦{compensation:,.2f}."
                ),
                notification_type="CANCELLED",
                related_object_id=app.id,
            )

        # Reject all PENDING applications
        pending_apps = ShiftApplication.objects.filter(shift=shift, status='PENDING')
        for app in pending_apps:
            app.status = 'REJECTED'
            app.cancellation_reason = 'Shift deleted by facility'
            app.save(update_fields=['status', 'cancellation_reason', 'updated_at'])

        # Refund remaining budget to facility
        total_confirmed_comp = cost_per_slot * Decimal('0.40') * confirmed_apps.count()
        total_original_cost = cost_per_slot * shift.quantity_needed
        refund = total_original_cost - total_confirmed_comp
        if refund > 0:
            shift.facility.wallet_balance += refund
            shift.facility.save(update_fields=['wallet_balance'])

            Transaction.objects.create(
                user=user, amount=refund,
                transaction_type='REFUND', reference=str(uuid.uuid4()),
                status='SUCCESS', shift=shift,
            )

        shift.status = 'CANCELLED'
        shift.save(update_fields=['status', 'updated_at'])

        return {
            "status": "success",
            "message": f"Shift deleted. Refund: ₦{refund:,.2f}. "
                       f"{confirmed_apps.count()} confirmed professional(s) compensated."
        }


# ---------------------------------------------------------------------------
# Facility ends a shift early (shift already in progress)
# ---------------------------------------------------------------------------

class FacilityEndShiftEarlyService(BaseService):
    """
    End an active shift early. Professionals are paid for hours worked.
    If hours worked < 60% of total scheduled hours, an extra 20% bonus is added.
    """

    @transaction.atomic
    def __call__(self, user, shift_id, reason=''):
        if not user.is_facility:
            raise PermissionError("Only facilities can end shifts.")

        shift = Shift.objects.select_for_update().get(id=shift_id)
        if shift.facility != user.facility:
            raise PermissionError("Not your shift.")

        now = timezone.now()
        if shift.start_time > now:
            raise ValueError("This shift hasn't started yet. Use 'Delete Shift' instead.")

        if shift.status in ('COMPLETED', 'CANCELLED'):
            raise ValueError("This shift is already completed or cancelled.")

        scheduled_hours = (shift.end_time - shift.start_time).total_seconds() / 3600
        payout_details = []

        # Process IN_PROGRESS applications (professionals currently working)
        active_apps = ShiftApplication.objects.filter(
            shift=shift, status='IN_PROGRESS'
        ).select_related('professional__user')

        for app in active_apps:
            clock_in = app.clock_in_time or shift.start_time
            worked_hours = max((now - clock_in).total_seconds() / 3600, 0)
            worked_pct = worked_hours / scheduled_hours if scheduled_hours > 0 else 1

            base_pay = shift.rate * Decimal(str(worked_hours))

            # Bonus: if worked less than 60% of scheduled time, add 20% bonus
            if worked_pct < 0.60:
                bonus = base_pay * Decimal('0.20')
            else:
                bonus = Decimal('0')

            total_pay = base_pay + bonus

            # Pay professional
            app.professional.wallet_balance += total_pay
            app.professional.save(update_fields=['wallet_balance'])

            Transaction.objects.create(
                user=app.professional.user, amount=total_pay,
                transaction_type='PAYOUT', reference=str(uuid.uuid4()),
                status='SUCCESS', shift=shift,
            )

            # Complete the application
            app.clock_out_time = now
            app.status = 'COMPLETED'
            app.save(update_fields=['status', 'clock_out_time', 'updated_at'])

            bonus_note = f" (includes 20% early-end bonus)" if bonus > 0 else ""
            Notification.send(
                user=app.professional.user,
                title="Shift Ended Early by Facility",
                message=(
                    f"The shift '{shift.role}' at {shift.facility.name} has been ended early. "
                    f"You've been paid ₦{total_pay:,.2f} for {worked_hours:.1f} hours{bonus_note}."
                ),
                notification_type="SHIFT_APPROVED",
                related_object_id=app.id,
            )

            payout_details.append({
                "professional": app.professional.user.email,
                "hours_worked": round(worked_hours, 2),
                "base_pay": str(base_pay),
                "bonus": str(bonus),
                "total_pay": str(total_pay),
            })

        # Cancel CONFIRMED applications that haven't clocked in yet — 40% compensation
        confirmed_apps = ShiftApplication.objects.filter(
            shift=shift, status='CONFIRMED'
        ).select_related('professional__user')

        for app in confirmed_apps:
            cost = shift.rate * Decimal(str(scheduled_hours))
            compensation = cost * Decimal('0.40')

            app.professional.wallet_balance += compensation
            app.professional.save(update_fields=['wallet_balance'])

            Transaction.objects.create(
                user=app.professional.user, amount=compensation,
                transaction_type='PAYOUT', reference=str(uuid.uuid4()),
                status='SUCCESS', shift=shift,
            )

            app.status = 'CANCELLED'
            app.cancelled_by = 'FACILITY'
            app.cancellation_reason = reason or 'Shift ended early by facility'
            app.save()

            Notification.send(
                user=app.professional.user,
                title="Shift Ended Early",
                message=(
                    f"The shift '{shift.role}' at {shift.facility.name} was ended early before "
                    f"you clocked in. You've been compensated ₦{compensation:,.2f}."
                ),
                notification_type="CANCELLED",
                related_object_id=app.id,
            )

        # Reject remaining PENDING
        ShiftApplication.objects.filter(
            shift=shift, status='PENDING'
        ).update(status='REJECTED')

        # Calculate facility refund (unused portion)
        total_original_cost = shift.rate * Decimal(str(scheduled_hours)) * shift.quantity_needed
        total_paid_out = sum(Decimal(p['total_pay']) for p in payout_details)
        total_confirmed_comp = sum(
            shift.rate * Decimal(str(scheduled_hours)) * Decimal('0.40')
            for _ in confirmed_apps
        )
        total_spent = total_paid_out + total_confirmed_comp
        refund = total_original_cost - total_spent
        if refund > 0:
            shift.facility.wallet_balance += refund
            shift.facility.save(update_fields=['wallet_balance'])

            Transaction.objects.create(
                user=user, amount=refund,
                transaction_type='REFUND', reference=str(uuid.uuid4()),
                status='SUCCESS', shift=shift,
            )

        shift.status = 'COMPLETED'
        shift.save(update_fields=['status', 'updated_at'])

        return {
            "status": "success",
            "message": f"Shift ended early. {len(payout_details)} professional(s) paid. "
                       f"Refund: ₦{refund:,.2f}.",
            "payouts": payout_details,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pro_display_name(professional):
    name = (
        f"{professional.user.first_name} {professional.user.last_name}".strip()
    )
    return name or professional.user.email
