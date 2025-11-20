from core.services import BaseService
from .models import ShiftApplication
from core.models import Notification
from django.utils import timezone

class ApproveShiftStartService(BaseService):
    def __call__(self, user, application_id):
        if not user.is_facility:
            raise PermissionError("Only facilities can approve shift start.")
            
        try:
            application = ShiftApplication.objects.get(id=application_id)
        except ShiftApplication.DoesNotExist:
            raise ValueError("Application not found.")
            
        if application.shift.facility.user != user:
            raise PermissionError("Not your shift.")
            
        if application.status != 'ATTENDANCE_PENDING':
            raise ValueError("Application is not pending attendance approval.")
            
        application.status = 'IN_PROGRESS'
        application.save()
        
        # Notify Professional
        Notification.objects.create(
            user=application.professional.user,
            title="Shift Started",
            message=f"Your start for '{application.shift.role}' has been approved.",
            notification_type="SHIFT_APPROVED",
            related_object_id=application.id
        )
        
        return application
