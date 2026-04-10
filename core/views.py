from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from core.router import route
from core.models import Notification, DeviceToken

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


@route("devices/register/", name="device-register")
class DeviceTokenRegisterView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get('token')
        device_type = request.data.get('device_type', 'android')

        if not token:
            return Response({"error": "token is required"}, status=400)
        if device_type not in ('android', 'ios', 'web'):
            return Response({"error": "device_type must be android, ios, or web"}, status=400)

        # Upsert: if token exists for another user, reassign it
        DeviceToken.objects.filter(token=token).exclude(user=request.user).delete()

        obj, created = DeviceToken.objects.update_or_create(
            user=request.user,
            token=token,
            defaults={'device_type': device_type, 'is_active': True},
        )
        return Response({"status": "registered", "created": created})


@route("devices/unregister/", name="device-unregister")
class DeviceTokenUnregisterView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get('token')
        if not token:
            return Response({"error": "token is required"}, status=400)

        deleted, _ = DeviceToken.objects.filter(user=request.user, token=token).delete()
        return Response({"status": "unregistered", "deleted": deleted > 0})
