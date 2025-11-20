import os
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
import communications.routing

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'shifta_project.settings')

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(
            communications.routing.websocket_urlpatterns
        )
    ),
})
