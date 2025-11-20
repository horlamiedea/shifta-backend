from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from core.router import route
from core.models import Notification

@route("notifications/", name="notification-list")
class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
        data = [{
            "id": notification.id,
            "title": notification.title,
            "message": notification.message,
            "type": notification.notification_type,
            "is_read": notification.is_read,
            "created_at": notification.created_at,
            "related_object_id": notification.related_object_id
        } for notification in notifications]
        return Response(data)

@route("notifications/<uuid:notification_id>/read/", name="notification-read")
class NotificationReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, notification_id):
        try:
            notification = Notification.objects.get(id=notification_id, user=request.user)
            notification.is_read = True
            notification.save()
            return Response({"status": "marked_read"})
        except Notification.DoesNotExist:
            return Response({"error": "Notification not found"}, status=404)
