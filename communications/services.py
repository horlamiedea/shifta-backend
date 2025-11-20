from core.services import BaseService
from shifts.models import Shift
from communications.models import ChatRoom, Message
from core.models import Notification
from django.db import transaction

class SendBroadcastService(BaseService):
    @transaction.atomic
    def __call__(self, user, shift_id, message_content):
        if not user.is_facility:
            raise PermissionError("Only facilities can send broadcasts.")
            
        try:
            shift = Shift.objects.get(id=shift_id)
        except Shift.DoesNotExist:
            raise ValueError("Shift not found.")
            
        if shift.facility.user != user:
            raise PermissionError("Not your shift.")
            
        # Find all confirmed applications
        applications = shift.applications.filter(status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'])
        
        if not applications.exists():
            return {"status": "no_recipients", "message": "No confirmed professionals for this shift."}
            
        count = 0
        for app in applications:
            # Ensure ChatRoom exists (it should, but let's be safe)
            chat_room, created = ChatRoom.objects.get_or_create(application=app)
            
            # Create Message
            Message.objects.create(
                room=chat_room,
                sender=user,
                content=f"[BROADCAST]: {message_content}",
                is_read=False
            )
            
            # Create Notification
            Notification.objects.create(
                user=app.professional.user,
                title=f"Broadcast from {shift.facility.name}",
                message=message_content,
                notification_type="BROADCAST",
                related_object_id=shift.id
            )
            count += 1
            
        return {"status": "success", "recipients_count": count}
