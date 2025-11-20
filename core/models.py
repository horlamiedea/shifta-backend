from django.db import models
import uuid

class BaseModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

class Notification(BaseModel):
    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=255)
    message = models.TextField()
    notification_type = models.CharField(max_length=50) # e.g., 'SHIFT_START', 'SHIFT_APPLICATION'
    is_read = models.BooleanField(default=False)
    related_object_id = models.UUIDField(null=True, blank=True) # Generic link to related object
    
    def __str__(self):
        return f"{self.title} - {self.user.email}"
