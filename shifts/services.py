from django.db import transaction
from core.services import BaseService
from core.geocoding import geocoding_service
from .models import Shift, ShiftApplication
from .tasks import notify_matching_professionals
from decimal import Decimal

class ShiftCreateService(BaseService):
    @transaction.atomic
    def __call__(self, user, role, specialty, quantity_needed, start_time, end_time, rate, is_negotiable=False, min_rate=None, address=None, latitude=None, longitude=None):
        if not user.is_facility:
            raise PermissionError("Only facilities can create shifts.")
        
        facility = user.facility
        
        if not facility.is_verified:
            raise PermissionError("Facility must be verified to create shifts. Please upload your documents.")
        
        # Cost Calculation & Deduction (Phase 2)
        # Duration in hours
        duration = (end_time - start_time).total_seconds() / 3600
        if duration <= 0:
             raise ValueError("End time must be after start time.")
             
        # Rate Validation (User requirement: "price cannot be set below average")
        # For now, let's assume a hardcoded average or use min_rate if provided
        AVERAGE_RATE = Decimal('2000.00') # Placeholder average rate
        if rate < AVERAGE_RATE:
            raise ValueError(f"Rate cannot be below the average rate of {AVERAGE_RATE}")

        # Rate is per hour per professional (as per user requirement: "put the amount for one hour... price will be 30*8")
        # So total_cost = rate * duration * quantity
        total_cost = rate *  Decimal(duration) * quantity_needed
        
        # Check Wallet Balance
        if facility.wallet_balance < total_cost:
             raise ValueError(f"Insufficient wallet balance. Required: {total_cost}, Available: {facility.wallet_balance}")
        
        # Deduct from Wallet
        facility.wallet_balance -= total_cost
        facility.save()
        
        # Handle location - use provided values or fallback to facility location
        shift_address = address
        shift_latitude = latitude
        shift_longitude = longitude
        
        # If no location provided, use facility's address and geocode it
        if not shift_address and not (shift_latitude and shift_longitude):
            # Use facility address as default
            shift_address = facility.address
            
            # Check if facility already has coordinates
            if facility.location_lat and facility.location_lng:
                shift_latitude = facility.location_lat
                shift_longitude = facility.location_lng
            elif facility.address:
                # Geocode facility address to get coordinates
                geocode_result = geocoding_service.geocode_address(facility.address)
                if geocode_result.get('success'):
                    shift_latitude = geocode_result['lat']
                    shift_longitude = geocode_result['lng']
                    shift_address = geocode_result.get('formatted_address', facility.address)
                    
                    # Also update facility with coordinates for future use
                    facility.location_lat = shift_latitude
                    facility.location_lng = shift_longitude
                    facility.save()
        
        # If address provided but no coordinates, geocode it
        elif shift_address and not (shift_latitude and shift_longitude):
            geocode_result = geocoding_service.geocode_address(shift_address)
            if geocode_result.get('success'):
                shift_latitude = geocode_result['lat']
                shift_longitude = geocode_result['lng']
                shift_address = geocode_result.get('formatted_address', shift_address)
        
        shift = Shift.objects.create(
            facility=facility,
            role=role,
            specialty=specialty,
            quantity_needed=quantity_needed,
            quantity_filled=0,
            start_time=start_time,
            end_time=end_time,
            rate=rate, # Storing hourly rate
            is_negotiable=is_negotiable,
            min_rate=min_rate,
            address=shift_address,
            latitude=shift_latitude,
            longitude=shift_longitude
        )
        
        # Trigger notification task
        notify_matching_professionals.delay(shift.id)
        
        return shift


class ShiftApplyService(BaseService):
    def __call__(self, user, shift_id):
        if not user.is_professional:
            raise PermissionError("Only professionals can apply.")
            
        shift = Shift.objects.get(id=shift_id)
        if shift.status != 'OPEN':
            raise ValueError("Shift is not open.")
            
        if ShiftApplication.objects.filter(shift=shift, professional=user.professional).exists():
            raise ValueError("Already applied.")
            
        # Clash Prevention (Phase 4)
        # Check for any CONFIRMED or IN_PROGRESS application that overlaps with this shift's time
        clashing_apps = ShiftApplication.objects.filter(
            professional=user.professional,
            status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'],
            shift__start_time__lt=shift.end_time,
            shift__end_time__gt=shift.start_time
        )
        
        if clashing_apps.exists():
            raise ValueError("This shift clashes with another shift you have accepted.")
            
        application = ShiftApplication.objects.create(
            shift=shift,
            professional=user.professional
        )
        return application

class ShiftManageApplicationService(BaseService):
    @transaction.atomic
    def __call__(self, user, application_id, action):
        if not user.is_facility:
            raise PermissionError("Only facilities can manage applications.")
            
        application = ShiftApplication.objects.get(id=application_id)
        if application.shift.facility != user.facility:
            raise PermissionError("Not your shift.")
            
        if action == 'CONFIRM':
            if application.shift.quantity_filled >= application.shift.quantity_needed:
                raise ValueError("Shift is already filled.")
                
            application.status = 'CONFIRMED'
            application.save()
            
            # Update shift filled count
            shift = application.shift
            shift.quantity_filled += 1
            if shift.quantity_filled >= shift.quantity_needed:
                shift.status = 'FILLED'
            shift.save()
            
        elif action == 'REJECT':
            application.status = 'REJECTED'
            application.save()
            
        return application

class ClockInService(BaseService):
    def __call__(self, user, shift_id, lat, lng, qr_code_data):
        # 1. Verify User is Professional
        if not user.is_professional:
            raise PermissionError("Only professionals can clock in.")
            
        # 2. Get Application
        try:
            application = ShiftApplication.objects.get(shift__id=shift_id, professional=user.professional, status='CONFIRMED')
        except ShiftApplication.DoesNotExist:
            raise ValueError("No confirmed application for this shift.")
            
        # 3. Verify QR Code (Simple check: qr_code_data == facility_id or similar)
        # PRD: "unique QR/Barcode for my facility"
        if qr_code_data != str(application.shift.facility.id):
             raise ValueError("Invalid Facility QR Code.")
          # 4. Geo-fencing Check
        # Use Shift location if available, else Facility location
        from core.utils import haversine
        facility = application.shift.facility # Get facility once
        target_lat = application.shift.latitude or facility.location_lat
        target_lng = application.shift.longitude or facility.location_lng
        
        if target_lat is None or target_lng is None:
             # If no location set, maybe skip check or enforce facility to set it?
             # For now, let's assume location is mandatory or skip if missing.
             pass
        else:
            distance = haversine(lat, lng, target_lat, target_lng)
            if distance > 2.0: # 2km radius as per Phase 3 requirement
                raise ValueError(f"You are too far from the shift location. Distance: {distance:.2f}km")
        
        # 5. Clock In
        from django.utils import timezone
        application.clock_in_time = timezone.now()
        application.status = 'ATTENDANCE_PENDING' # Phase 3: Needs approval
        application.save()
        
        # 6. Notify Facility
        from core.models import Notification
        Notification.objects.create(
            user=facility.user,
            title="Shift Start Request",
            message=f"{user.email} has started the shift '{application.shift.role}'. Please approve.",
            notification_type="SHIFT_START",
            related_object_id=application.id
        )
        
        return application

class ClockOutService(BaseService):
    def __call__(self, user, shift_id, lat, lng, qr_code_data):
        # Similar checks...
        if not user.is_professional:
            raise PermissionError("Only professionals can clock out.")
            
        try:
            application = ShiftApplication.objects.get(shift__id=shift_id, professional=user.professional, status='CONFIRMED')
        except ShiftApplication.DoesNotExist:
            raise ValueError("No confirmed application for this shift.")
            
        if qr_code_data != str(application.shift.facility.id):
             raise ValueError("Invalid Facility QR Code.")
             
        from core.utils import haversine
        distance = haversine(lat, lng, application.shift.facility.location_lat, application.shift.facility.location_lng)
        if distance > 0.5:
            raise ValueError("You must be at the facility to clock out.")
            
        from django.utils import timezone
        application.clock_out_time = timezone.now()
        application.save()
        
        # Trigger Payment (Epic 6)
        from billing.tasks import payout_professional
        # Schedule for 24 hours later as per PRD
        payout_professional.apply_async((application.id,), countdown=24*3600)
        
        return application

from .models import ExtraTimeRequest
from django.utils import timezone

class ExtraTimeService(BaseService):
    def request_extra_time(self, user, shift_application_id, hours, reason):
        if not user.is_professional:
             raise PermissionError("Only professionals can request extra time.")
             
        application = ShiftApplication.objects.get(id=shift_application_id)
        if application.professional.user != user:
            raise PermissionError("Not your shift application.")
            
        request = ExtraTimeRequest.objects.create(
            shift_application=application,
            hours=hours,
            reason=reason,
            status='PENDING',
            requested_by=user
        )
        
        # Notify Facility
        from core.models import Notification
        Notification.objects.create(
            user=application.shift.facility.user,
            title="Extra Time Request",
            message=f"{user.email} requested {hours}hrs extra time for '{application.shift.role}'.",
            notification_type="REMINDER", # Using REMINDER as generic for now, or add EXTRA_TIME_REQUEST type
            data={"request_id": str(request.id)}
        )
        
        return request

    def add_extra_time(self, user, shift_application_id, hours, reason):
        if not user.is_facility:
             # Check staff permissions
             if hasattr(user, 'facility_staff_profile') and user.facility_staff_profile.can_create_shifts: # Reuse create permission?
                 facility = user.facility_staff_profile.facility
             else:
                 raise PermissionError("Permission denied.")
        else:
            facility = user.facility
            
        application = ShiftApplication.objects.get(id=shift_application_id)
        if application.shift.facility != facility:
            raise PermissionError("Not your shift.")
            
        request = ExtraTimeRequest.objects.create(
            shift_application=application,
            hours=hours,
            reason=reason,
            status='APPROVED',
            requested_by=user,
            approved_by=user,
            approved_at=timezone.now()
        )
        
        # Notify Professional
        from core.models import Notification
        Notification.objects.create(
            user=application.professional.user,
            title="Extra Time Added",
            message=f"{facility.name} added {hours}hrs extra time to your shift.",
            notification_type="SHIFT_APPROVED", # Reusing type
            data={"request_id": str(request.id)}
        )
        
        return request

    def approve_extra_time(self, user, request_id):
        if not user.is_facility:
             if hasattr(user, 'facility_staff_profile') and user.facility_staff_profile.can_create_shifts:
                 facility = user.facility_staff_profile.facility
             else:
                 raise PermissionError("Permission denied.")
        else:
            facility = user.facility
            
        request = ExtraTimeRequest.objects.get(id=request_id)
        if request.shift_application.shift.facility != facility:
            raise PermissionError("Not your shift.")
            
        request.status = 'APPROVED'
        request.approved_by = user
        request.approved_at = timezone.now()
        request.save()
        
        # Notify Professional
        from core.models import Notification
        Notification.objects.create(
            user=request.shift_application.professional.user,
            title="Extra Time Approved",
            message=f"Your request for {request.hours}hrs extra time has been approved.",
            notification_type="SHIFT_APPROVED",
            data={"request_id": str(request.id)}
        )
        
        return request

