from rest_framework.views import exception_handler

def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)

    if response is not None:
        custom_data = {
            "success": False,
            "status_code": response.status_code,
            "message": "Validation failed" if response.status_code == 400 else str(exc),
            "errors": response.data,
            "data": None
        }
        response.data = custom_data

    return response
