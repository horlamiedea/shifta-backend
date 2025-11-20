from celery import shared_task
from django.db import transaction
from shifts.models import ShiftApplication
from .models import Transaction
import uuid
from decimal import Decimal

@shared_task
def payout_professional(application_id):
    try:
        application = ShiftApplication.objects.get(id=application_id)
    except ShiftApplication.DoesNotExist:
        return

    if application.status != 'CONFIRMED' or not application.clock_out_time:
        return # Not eligible

    professional = application.professional
    shift = application.shift
    
    # Calculate Amount: Hourly Rate * Duration
    duration = (shift.end_time - shift.start_time).total_seconds() / 3600
    amount = shift.rate * Decimal(duration)
    
    # 1. Call Paystack API (Mocked) - Transfer to Bank?
    # User said: "Professional can make withdrawal".
    # So here we just credit their wallet. Withdrawal is a separate action.
    
    with transaction.atomic():
        # Credit Professional Wallet
        professional.wallet_balance += amount
        professional.save()
        
        # Create Transaction Record (Credit)
        Transaction.objects.create(
            user=professional.user,
            amount=amount,
            transaction_type='PAYOUT', # Or 'EARNING'
            reference=str(uuid.uuid4()),
            status='SUCCESS',
            shift=shift
        )
    
    print(f"Credited {amount} to {professional.user.email} wallet.")
