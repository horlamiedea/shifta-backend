from django.db import models
import uuid

class BaseModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

class Notification(BaseModel):
    NOTIFICATION_TYPES = (
        ('SHIFT_POSTED', 'Shift Posted'),
        ('SHIFT_APPROVED', 'Shift Approved'),
        ('REMINDER', 'Reminder'),
        ('CANCELLED', 'Shift Cancelled'),
        ('BOOKED', 'Shift Booked'),
        ('MESSAGE', 'New Message'),
        ('INVOICE_UPCOMING', 'Invoice Upcoming'),
        ('INVOICE_GENERATED', 'Invoice Generated'),
        ('BROADCAST', 'Broadcast'),
    )

    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=255)
    message = models.TextField()
    notification_type = models.CharField(max_length=50, choices=NOTIFICATION_TYPES)
    is_read = models.BooleanField(default=False)
    related_object_id = models.UUIDField(null=True, blank=True) # Generic link to related object
    data = models.JSONField(default=dict, blank=True) # For extra context
    
    def __str__(self):
        return f"{self.title} - {self.user.email}"
