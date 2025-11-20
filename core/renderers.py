from rest_framework.renderers import JSONRenderer

class StandardResponseRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        status_code = renderer_context['response'].status_code
        response_data = {
            "success": 200 <= status_code < 300,
            "status_code": status_code,
            "message": "Request successful" if 200 <= status_code < 300 else "Request failed",
            "data": data,
        }

        # If data contains 'message', use it and remove from data
        if isinstance(data, dict):
            if "message" in data:
                response_data["message"] = data.pop("message")
            if "errors" in data:
                response_data["errors"] = data.pop("errors")
                response_data["success"] = False
            
            # If the original data was just message/errors, 'data' might be empty or partial.
            # Adjust logic to ensure 'data' field contains the actual payload.
            # However, for error responses, 'data' should be null as per requirements.
            if not response_data["success"]:
                response_data["data"] = None
            
        return super().render(response_data, accepted_media_type, renderer_context)
