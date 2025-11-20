from django.db import models
from accounts.models import Facility, Professional, User
from shifts.models import Shift
from core.models import BaseModel

class Transaction(BaseModel):
    TRANSACTION_TYPES = (
        ('PAYOUT', 'Payout'),
        ('CHARGE', 'Charge'),
        ('REFUND', 'Refund'),
    )
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    reference = models.CharField(max_length=100, unique=True) # Paystack reference
    status = models.CharField(max_length=20, default='PENDING') # PENDING, SUCCESS, FAILED
    # created_at in BaseModel
    shift = models.ForeignKey(Shift, on_delete=models.SET_NULL, null=True, blank=True)
    
    def __str__(self):
        return f"{self.transaction_type} - {self.amount} - {self.status}"

class Invoice(BaseModel):
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='invoices')
    month = models.DateField() # First day of the month
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, default='PENDING') # PENDING, PAID
    pdf_url = models.URLField(null=True, blank=True)
    # created_at in BaseModel
    
    def __str__(self):
        return f"Invoice for {self.facility.name} - {self.month}"

class AdminWalletLog(BaseModel):
    admin_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='wallet_logs')
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='wallet_logs')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    comment = models.TextField()
    
    def __str__(self):
        return f"{self.admin_user} funded {self.facility} - {self.amount}"
