from celery import shared_task
from accounts.models import Professional
from core.utils import haversine
from .models import Shift

@shared_task
def notify_matching_professionals(shift_id):
    try:
        shift = Shift.objects.get(id=shift_id)
    except Shift.DoesNotExist:
        return

    facility_lat = shift.facility.location_lat
    facility_lng = shift.facility.location_lng
    
    if not facility_lat or not facility_lng:
        return

    # Find professionals with matching specialty
    # In a real app with PostGIS, we'd do this in the DB query.
    # Here we fetch matching specialties and filter by distance and availability in Python.
    
    # Initial filter for professionals based on specialty and verification
    potential_candidates = Professional.objects.filter(
        specialties__contains=[shift.specialty], # Assuming JSON list
        is_verified=True,
        current_location_lat__isnull=False,
        current_location_lng__isnull=False
    )
    
    matching_pros = []
    for pro in potential_candidates:
        # Location Check (20km radius)
        # Use shift location if available, otherwise facility location
        target_lat = shift.latitude if shift.latitude is not None else facility_lat
        target_lng = shift.longitude if shift.longitude is not None else facility_lng
        
        if target_lat is not None and target_lng is not None:
            dist = haversine(pro.current_location_lat, pro.current_location_lng, target_lat, target_lng)
            
            if dist <= 20: # 20km radius
                # Clash Check: Check if pro has any overlapping confirmed shift
                has_clash = ShiftApplication.objects.filter(
                    professional=pro,
                    status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'],
                    shift__start_time__lt=shift.end_time,
                    shift__end_time__gt=shift.start_time
                ).exists()
                
                if not has_clash:
                    matching_pros.append(pro)
    
    # Send notifications
    for pro in matching_pros:
        # Mock sending Push/SMS
        print(f"Sending notification to {pro.user.email}: New shift available at {shift.facility.name}")
```
