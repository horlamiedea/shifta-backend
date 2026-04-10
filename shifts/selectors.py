from core.services import BaseSelector
from .models import Shift, ShiftApplication

class ShiftSelector(BaseSelector):
    def list_open_shifts(self, specialty=None, exclude_professional=None):
        qs = Shift.objects.filter(status__in=['OPEN', 'FILLED'])
        if specialty:
            qs = qs.filter(specialty=specialty)
        if exclude_professional:
            applied_shift_ids = ShiftApplication.objects.filter(
                professional=exclude_professional
            ).values_list('shift_id', flat=True)
            qs = qs.exclude(id__in=applied_shift_ids)
        return qs.order_by('-created_at')

    def list_facility_shifts(self, facility, status=None):
        qs = Shift.objects.filter(facility=facility)
        if status:
            qs = qs.filter(status=status.upper())
        return qs.order_by('-created_at')

    def list_professional_shifts(self, professional):
        # Exclude shifts the professional already applied to
        applied_shift_ids = ShiftApplication.objects.filter(
            professional=professional
        ).values_list('shift_id', flat=True)
        qs = (
            Shift.objects.filter(status__in=['OPEN', 'FILLED'])
            .exclude(id__in=applied_shift_ids)
            .select_related('facility')
        )
        return qs.order_by('-created_at')

    def get_shift(self, shift_id):
        return Shift.objects.get(id=shift_id)
        
    def list_applications(self, shift_id, user):
        shift = Shift.objects.get(id=shift_id)
        if shift.facility.user != user:
            raise PermissionError("Not your shift.")
        return (
            ShiftApplication.objects
            .filter(shift=shift)
            .select_related('professional__user', 'review')
            .order_by('-created_at')
        )

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

    def list_facility_pending_applications(self, facility):
        return (
            ShiftApplication.objects
            .filter(shift__facility=facility, status='PENDING')
            .select_related('shift', 'professional__user')
            .order_by('-created_at')
        )
