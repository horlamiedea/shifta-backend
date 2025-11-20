from django.urls import path
from rest_framework.routers import DefaultRouter

class EndpointRegistry:
    def __init__(self):
        self._registry = []

    def route(self, path_str, name=None, methods=None):
        def decorator(view_class):
            self._registry.append({
                "path": path_str,
                "view": view_class,
                "name": name or view_class.__name__.lower(),
                "methods": methods
            })
            return view_class
        return decorator

    def get_urls(self):
        urlpatterns = []
        for entry in self._registry:
            urlpatterns.append(
                path(entry["path"], entry["view"].as_view(), name=entry["name"])
            )
        return urlpatterns

registry = EndpointRegistry()
route = registry.route
