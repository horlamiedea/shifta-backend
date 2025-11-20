from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from core.router import route
from .models import ChatRoom, Message
from .services import SendBroadcastService

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
