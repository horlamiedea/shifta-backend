from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from .models import Professional

@shared_task
def check_license_expiry():
    today = timezone.now().date()
    
    # Deactivate expired licenses
    expired_pros = Professional.objects.filter(license_expiry_date__lt=today, is_verified=True)
    for pro in expired_pros:
        pro.is_verified = False # Or use a separate is_active field if needed
        pro.save()
        # TODO: Send notification "Your license has expired"

    # Send warnings (60, 30, 7 days)
    # This logic would typically send notifications.
    # For now we just log or pass.
    pass
