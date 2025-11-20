from django.db import models
from accounts.models import User
from shifts.models import ShiftApplication
from core.models import BaseModel

class ChatRoom(BaseModel):
    application = models.OneToOneField(ShiftApplication, on_delete=models.CASCADE, related_name='chat_room')
    # created_at in BaseModel
    
    def __str__(self):
        return f"Chat for {self.application}"

class Message(BaseModel):
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_messages')
    content = models.TextField()
    # timestamp replaced by created_at
    is_read = models.BooleanField(default=False)
    
    def __str__(self):
        return f"{self.sender} at {self.created_at}"
