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

    @classmethod
    def send(cls, user, title, message, notification_type, related_object_id=None, data=None):
        """Create a notification and send a push notification to the user's devices."""
        notif = cls.objects.create(
            user=user,
            title=title,
            message=message,
            notification_type=notification_type,
            related_object_id=related_object_id,
            data=data or {},
        )
        # Fire-and-forget push — don't block if Firebase isn't configured
        try:
            from .push import send_push_to_user
            send_push_to_user(
                user=user,
                title=title,
                body=message,
                data={
                    'notification_id': str(notif.id),
                    'type': notification_type,
                    **(data or {}),
                },
            )
        except Exception:
            pass  # Push is best-effort
        return notif


class DeviceToken(BaseModel):
    DEVICE_TYPES = (
        ('android', 'Android'),
        ('ios', 'iOS'),
        ('web', 'Web'),
    )

    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='device_tokens')
    token = models.TextField(unique=True)
    device_type = models.CharField(max_length=10, choices=DEVICE_TYPES)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('user', 'token')

    def __str__(self):
        return f"{self.user.email} - {self.device_type}"
