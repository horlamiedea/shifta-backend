from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from core.router import route
from .services import ShiftCreateService, ShiftApplyService, ShiftManageApplicationService, ClockInService, ClockOutService
from .cancellation_services import FacilityCancelShiftService, ProfessionalCancelShiftService
from .approval_services import ApproveShiftStartService
from .selectors import ShiftSelector

@route("shifts/", name="shift-list-create")
class ShiftListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        selector = ShiftSelector()
        # Filter by specialty if provided
        specialty = request.query_params.get("specialty")
        shifts = selector.list_open_shifts(specialty=specialty)
        
        data = [{
            "id": str(s.id),
            "facility": s.facility.name,
            "role": s.role,
            "specialty": s.specialty,
            "start_time": s.start_time,
            "rate": s.rate
        } for s in shifts]
        
        return Response(data)

    def post(self, request):
        service = ShiftCreateService()
        shift = service(
            user=request.user,
            role=request.data.get("role"),
            specialty=request.data.get("specialty"),
            quantity_needed=request.data.get("quantity_needed"),
            start_time=request.data.get("start_time"),
            end_time=request.data.get("end_time"),
            rate=request.data.get("rate")
        )
        return Response({"id": shift.id, "status": "created"}, status=201)

@route("shifts/<uuid:shift_id>/apply/", name="shift-apply")
class ShiftApplyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, shift_id):
        service = ShiftApplyService()
        application = service(user=request.user, shift_id=shift_id)
        return Response({"status": "applied", "application_id": application.id})

@route("shifts/applications/<int:application_id>/manage/", name="shift-application-manage")
class ShiftApplicationManageView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, application_id):
        action = request.data.get("action") # CONFIRM or REJECT
        service = ShiftManageApplicationService()
        service(user=request.user, application_id=application_id, action=action)
        return Response({"status": "success"})

@route("facility/qrcode/", name="facility-qrcode")
class FacilityQRCodeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_facility:
            return Response({"error": "Only facilities have QR codes"}, status=403)
            
        # Return the facility ID as the QR code data
        # Frontend will generate the QR image from this string.
        return Response({"qr_data": str(request.user.facility.id)})

@route("shifts/<uuid:shift_id>/clock-in/", name="shift-clock-in")
class ShiftClockInView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, shift_id):
        lat = request.data.get("lat")
        lng = request.data.get("lng")
        qr_code_data = request.data.get("qr_code_data")
        
        service = ClockInService()
        service(user=request.user, shift_id=shift_id, lat=lat, lng=lng, qr_code_data=qr_code_data)
        
        return Response({"status": "clocked_in"})

@route("shifts/<uuid:shift_id>/clock-out/", name="shift-clock-out")
class ShiftClockOutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, shift_id):
        lat = request.data.get("lat")
        lng = request.data.get("lng")
        qr_code_data = request.data.get("qr_code_data")
        
        service = ClockOutService()
        service(user=request.user, shift_id=shift_id, lat=lat, lng=lng, qr_code_data=qr_code_data)
        
        return Response({"status": "clocked_out"})

@route("shifts/<uuid:shift_id>/cancel/", name="shift-cancel")
class ShiftCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, shift_id):
        if request.user.is_facility:
            professional_id = request.data.get("professional_id")
            service = FacilityCancelShiftService()
            result = service(user=request.user, shift_id=shift_id, professional_id=professional_id)
            return Response(result)
        elif request.user.is_professional:
            service = ProfessionalCancelShiftService()
            result = service(user=request.user, shift_id=shift_id)
            return Response(result)
        else:
            return Response({"error": "Invalid user role"}, status=403)

@route("shifts/facility/", name="facility-shift-list")
class FacilityShiftListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_facility:
            return Response({"error": "Only facilities can view this."}, status=403)
            
        selector = ShiftSelector()
        shifts = selector.list_facility_shifts(request.user.facility)
        
        data = [{
            "id": shift.id,
            "role": shift.role,
            "start_time": shift.start_time,
            "end_time": shift.end_time,
            "status": shift.status,
            "quantity_needed": shift.quantity_needed,
            "quantity_filled": shift.quantity_filled,
            "rate": shift.rate
        } for shift in shifts]
        
        return Response(data)

@route("shifts/professional/", name="professional-shift-list")
class ProfessionalShiftListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_professional:
            return Response({"error": "Only professionals can view this."}, status=403)
            
        selector = ShiftSelector()
        shifts = selector.list_professional_shifts(request.user.professional)
        
        data = [{
            "id": shift.id,
            "facility": shift.facility.name,
            "role": shift.role,
            "start_time": shift.start_time,
            "end_time": shift.end_time,
            "rate": shift.rate,
            "distance": "5km" # Placeholder or calculate
        } for shift in shifts]
        
        return Response(data)



