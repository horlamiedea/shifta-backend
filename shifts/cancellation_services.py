from django.db import transaction
from core.services import BaseService
from .models import Shift, ShiftApplication
from accounts.models import Review
from billing.models import Transaction
from decimal import Decimal
from django.utils import timezone
import uuid

class FacilityCancelShiftService(BaseService):
    @transaction.atomic
    def __call__(self, user, shift_id, professional_id=None):
        if not user.is_facility:
            raise PermissionError("Only facilities can cancel shifts.")
            
        shift = Shift.objects.get(id=shift_id)
        if shift.facility != user.facility:
            raise PermissionError("Not your shift.")
            
        # If professional_id is provided, we are removing a specific professional (cancelling their application)
        # If not, we are cancelling the entire shift (logic might differ, but user focused on "remove that profession")
        
        if professional_id:
            try:
                application = ShiftApplication.objects.get(shift=shift, professional__id=professional_id, status='CONFIRMED')
            except ShiftApplication.DoesNotExist:
                raise ValueError("No confirmed application for this professional.")
                
            if application.clock_in_time:
                raise ValueError("Cannot remove professional who has started the shift.")
                
            # Calculate Amounts
            duration = (shift.end_time - shift.start_time).total_seconds() / 3600
            total_cost = shift.rate * Decimal(duration)
            
            penalty_amount = total_cost * Decimal('0.10') # 10%
            compensation_amount = total_cost * Decimal('0.03') # 3%
            refund_amount = total_cost - penalty_amount # 90%
            
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
                transaction_type='PAYOUT', # Compensation
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
            
            return {"status": "success", "message": "Professional removed. Refund processed."}
            
        else:
            # Cancel entire shift?
            # For now, let's assume this service is for removing professionals as per the detailed requirement.
            # If cancelling entire shift, we'd loop through all confirmed applications and apply same logic.
            pass

class ProfessionalCancelShiftService(BaseService):
    @transaction.atomic
    def __call__(self, user, shift_id):
        if not user.is_professional:
            raise PermissionError("Only professionals can cancel.")
            
        try:
            application = ShiftApplication.objects.get(shift__id=shift_id, professional=user.professional, status='CONFIRMED')
        except ShiftApplication.DoesNotExist:
            raise ValueError("No confirmed application.")
            
        shift = application.shift
        now = timezone.now()
        
        # Check time for penalty
        # "if a shift is suppose to start by 8pm... anything after 4pm then it will affect their rating"
        # So cutoff is 4 hours before start.
        cutoff_time = shift.start_time - timezone.timedelta(hours=4)
        
        if now > cutoff_time:
            # Late Cancellation
            # 1-star rating + Auto-review
            Review.objects.create(
                reviewer=shift.facility.user, # System or Facility? User said "automatically". Let's attribute to Facility.
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
        
        return {"status": "success", "message": message}
