from django.db import transaction
from core.services import BaseService
from .models import Transaction
from accounts.models import User
import uuid

class WithdrawalService(BaseService):
    @transaction.atomic
    def __call__(self, user, amount):
        if not user.is_professional:
            raise PermissionError("Only professionals can withdraw.")
            
        professional = user.professional
        if professional.wallet_balance < amount:
            raise ValueError("Insufficient funds.")
            
        # Deduct from Wallet
        professional.wallet_balance -= amount
        professional.save()
        
        # Create Transaction
        Transaction.objects.create(
            user=user,
            amount=amount,
            transaction_type='PAYOUT', # Withdrawal
            reference=str(uuid.uuid4()),
            status='PENDING' # Pending bank processing
        )
        
        # Trigger Paystack Transfer (Mock)
        # ...
        
        return {"status": "success", "message": "Withdrawal initiated."}

class ReleaseFundsService(BaseService):
    def __call__(self, user, application_id):
        if not user.is_facility:
            raise PermissionError("Only facilities can release funds.")
            
        # Logic to find the pending payout task and execute it immediately?
        # Or just trigger the payout logic now and cancel the scheduled task?
        # Celery task revocation is tricky without task_id.
        # Alternative: The task checks a flag. Or we just run the logic here and set a flag "paid" on application.
        
        # Let's assume we run the logic here.
        # We need to import payout_professional logic or move it to a service.
        # Ideally, payout_professional task calls a service `ProcessPayoutService`.
        # Then we can call `ProcessPayoutService` here too.
        
        from .tasks import payout_professional
        # We can just call the task synchronously or with delay=0
        payout_professional.apply_async((application_id,), countdown=0)
        
        return {"status": "success", "message": "Funds released."}
