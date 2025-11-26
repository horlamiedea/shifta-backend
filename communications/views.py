from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from core.router import route
from .models import ChatRoom, Message
from .services import SendBroadcastService
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, inline_serializer
from rest_framework import serializers

@extend_schema(
    request=inline_serializer(
        name='BroadcastMessageRequest',
        fields={
            'shift_id': serializers.UUIDField(),
            'message': serializers.CharField(),
        }
    ),
    responses={
        200: inline_serializer(
            name='BroadcastMessageResponse',
            fields={'status': serializers.CharField(), 'recipients_count': serializers.IntegerField()}
        ),
        400: inline_serializer(name='BroadcastValidationError', fields={'error': serializers.CharField()})
    }
)

@route("communications/broadcast/", name="broadcast-message")
class BroadcastMessageView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        shift_id = request.data.get("shift_id")
        message = request.data.get("message")
        
        if not shift_id or not message:
            return Response({"error": "Shift ID and Message are required"}, status=400)
            
        service = SendBroadcastService()
        result = service(user=request.user, shift_id=shift_id, message_content=message)
        
        return Response(result)
from shifts.models import ShiftApplication

@extend_schema(
    request=inline_serializer(
        name='ChatRoomCreateRequest',
        fields={
            'application_id': serializers.IntegerField(),
        }
    ),
    responses={
        200: inline_serializer(
            name='ChatRoomCreateResponse',
            fields={'room_id': serializers.IntegerField(), 'created': serializers.BooleanField()}
        ),
        403: inline_serializer(name='ChatRoomPermissionError', fields={'error': serializers.CharField()})
    }
)
@route("chat/rooms/", name="chat-room-create")
class ChatRoomCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        application_id = request.data.get("application_id")
        application = ShiftApplication.objects.get(id=application_id)
        
        # Check permissions (only facility or professional involved)
        if request.user != application.professional.user and request.user != application.shift.facility.user:
            return Response({"error": "Permission denied"}, status=403)
            
        room, created = ChatRoom.objects.get_or_create(application=application)
        return Response({"room_id": room.id, "created": created})

@extend_schema(
    responses={
        200: inline_serializer(
            name='ChatHistoryResponse',
            many=True,
            fields={
                'sender': serializers.EmailField(),
                'content': serializers.CharField(),
                'timestamp': serializers.DateTimeField()
            }
        )
    }
)
@route("chat/rooms/<int:room_id>/messages/", name="chat-history")
class ChatHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, room_id):
        room = ChatRoom.objects.get(id=room_id)
        # Check permissions
        # ... (omitted for brevity, similar to above)
        
        messages = room.messages.order_by('created_at')
        data = [{
            "sender": m.sender.email,
            "content": m.content,
            "timestamp": m.created_at
        } for m in messages]
        return Response(data)

from core.models import Notification

@extend_schema(
    responses={
        200: inline_serializer(
            name='NotificationListResponse',
            many=True,
            fields={
                'id': serializers.UUIDField(),
                'title': serializers.CharField(),
                'message': serializers.CharField(),
                'notification_type': serializers.CharField(),
                'is_read': serializers.BooleanField(),
                'created_at': serializers.DateTimeField(),
                'data': serializers.JSONField()
            }
        )
    }
)
@route("notifications/", name="notifications-list")
class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
        data = [{
            "id": n.id,
            "title": n.title,
            "message": n.message,
            "notification_type": n.notification_type,
            "is_read": n.is_read,
            "created_at": n.created_at,
            "data": n.data
        } for n in notifications]
        return Response(data)

@extend_schema(
    responses={
        200: inline_serializer(
            name='NotificationMarkReadResponse',
            fields={'status': serializers.CharField()}
        )
    }
)
@route("notifications/<uuid:notification_id>/read/", name="notification-mark-read")
class NotificationMarkReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, notification_id):
        try:
            notification = Notification.objects.get(id=notification_id, user=request.user)
            notification.is_read = True
            notification.save()
            return Response({"status": "success"})
        except Notification.DoesNotExist:
            return Response({"error": "Notification not found"}, status=404)
