from django.db import models
from django.utils.translation import gettext_lazy as _
from accounts.models import Facility, Professional
from core.models import BaseModel
import uuid

class Shift(BaseModel):
    STATUS_CHOICES = (
        ('OPEN', 'Open'),
        ('FILLED', 'Filled'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    )
    
    # id and created_at are in BaseModel
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='shifts')
    role = models.CharField(max_length=100) # e.g. "Nurse"
    specialty = models.CharField(max_length=100) # e.g. "ICU"
    quantity_needed = models.IntegerField(default=1)
    quantity_filled = models.IntegerField(default=0)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    rate = models.DecimalField(max_digits=10, decimal_places=2) # Hourly rate per professional
    
    # Phase 3: Location
    address = models.TextField(null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN')
    
    def __str__(self):
        return f"{self.role} at {self.facility.name}"

class SavedAddress(BaseModel):
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='saved_addresses')
    name = models.CharField(max_length=255) # e.g., "Main Branch", "Annex"
    address = models.TextField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    
    def __str__(self):
        return f"{self.name} - {self.address}"

class ShiftApplication(BaseModel):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('CONFIRMED', 'Confirmed'),
        ('REJECTED', 'Rejected'),
        ('ATTENDANCE_PENDING', 'Attendance Pending'), # Phase 3: Clock-in needs approval
        ('IN_PROGRESS', 'In Progress'), # Clocked In & Approved
        ('COMPLETED', 'Completed'), # Clocked Out
        ('CANCELLED', 'Cancelled'),
    )
    
    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name='applications')
    professional = models.ForeignKey(Professional, on_delete=models.CASCADE, related_name='applications')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    # applied_at is replaced by created_at from BaseModel
    clock_in_time = models.DateTimeField(null=True, blank=True)
    clock_out_time = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        unique_together = ('shift', 'professional')
        
    def __str__(self):
        return f"{self.professional} applied for {self.shift}"
