from core.services import BaseSelector
from .models import Shift, ShiftApplication

class ShiftSelector(BaseSelector):
    def list_open_shifts(self, specialty=None):
        qs = Shift.objects.filter(status='OPEN')
        if specialty:
            qs = qs.filter(specialty=specialty)
        return qs

    def list_facility_shifts(self, facility):
        return Shift.objects.filter(facility=facility).order_by('-created_at')

    def list_professional_shifts(self, professional):
        # Filter by status OPEN
        qs = Shift.objects.filter(status='OPEN')
        
        # Filter by specialty (if professional has specialties)
        # Assuming professional.specialties is a list of strings
        if professional.specialties:
             # This is a JSONField, so filtering might be tricky depending on DB.
             # For simple "contains" logic:
             # qs = qs.filter(specialty__in=professional.specialties)
             # But Shift.specialty is a single string (CharField) based on my create service?
             # Let's check Shift model.
             pass
             
        # Filter by location (Radius) - Phase 1 implemented 20km radius in matching.
        # Here we can just return all or filter by distance if lat/lng provided.
        
        return qs.order_by('-created_at')

    def get_shift(self, shift_id):
        return Shift.objects.get(id=shift_id)
        
    def list_applications(self, shift_id, user):
        shift = Shift.objects.get(id=shift_id)
        if shift.facility.user != user:
            raise PermissionError("Not your shift.")
        return ShiftApplication.objects.filter(shift=shift)

    def list_calendar_shifts(self, facility, date_start, date_end, applicant_id=None):
        qs = Shift.objects.filter(facility=facility)
        
        # Filter by date range
        if date_start:
            qs = qs.filter(start_time__date__gte=date_start)
        if date_end:
            qs = qs.filter(start_time__date__lte=date_end)
            
        if applicant_id:
            # Filter shifts where specific applicant has applied and is confirmed
            qs = qs.filter(
                applications__professional__id=applicant_id, 
                applications__status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING', 'COMPLETED']
            )
            
        return qs.distinct()
