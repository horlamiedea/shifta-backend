from django.db import transaction
from core.services import BaseService
from core.geocoding import geocoding_service
from .models import Shift, ShiftApplication
from .tasks import notify_matching_professionals
from decimal import Decimal

class ShiftCreateService(BaseService):
    @transaction.atomic
    def __call__(self, user, role, specialty, quantity_needed, start_time, end_time, rate, is_negotiable=False, min_rate=None, address=None, latitude=None, longitude=None):
        from django.utils import timezone

        if not user.is_facility:
            raise PermissionError("Only facilities can create shifts.")

        facility = user.facility

        if not facility.is_verified:
            raise PermissionError("Facility must be verified to create shifts. Please upload your documents.")

        # Start time must be in the future
        if start_time <= timezone.now():
            raise ValueError("Start time must be in the future.")

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


class ShiftUpdateService(BaseService):
    """Allows a facility to edit an OPEN or FILLED shift (quantity, dates, rate)."""

    @transaction.atomic
    def __call__(self, user, shift_id, **kwargs):
        from django.utils import timezone

        if not user.is_facility:
            raise PermissionError("Only facilities can edit shifts.")

        shift = Shift.objects.select_for_update().get(id=shift_id)
        if shift.facility != user.facility:
            raise PermissionError("Not your shift.")

        if shift.status not in ('OPEN', 'FILLED'):
            raise ValueError("Only OPEN or FILLED shifts can be edited.")

        now = timezone.now()

        # Block edits once the shift has started
        if shift.start_time <= now:
            raise ValueError("This shift has already started and cannot be edited.")

        # Block edits if any professional is already IN_PROGRESS
        has_active = ShiftApplication.objects.filter(
            shift=shift, status='IN_PROGRESS'
        ).exists()
        if has_active:
            raise ValueError("This shift has active professionals and cannot be edited.")

        # For FILLED shifts, only allow increasing quantity (professionals already agreed to terms)
        if shift.status == 'FILLED':
            restricted = {'start_time', 'end_time', 'rate', 'min_rate'}
            changing_restricted = {k for k in restricted if k in kwargs and kwargs[k] is not None}
            if changing_restricted:
                raise ValueError(
                    "Cannot change time or rate on a filled shift — professionals have already been confirmed. "
                    "You can only increase the number of professionals needed."
                )

        # Track what changed for wallet adjustments
        old_quantity = shift.quantity_needed
        old_rate = shift.rate
        old_start = shift.start_time
        old_end = shift.end_time
        old_duration = (old_end - old_start).total_seconds() / 3600

        # Apply allowed field updates
        allowed_fields = ['quantity_needed', 'start_time', 'end_time', 'rate', 'is_negotiable', 'min_rate']
        for field in allowed_fields:
            if field in kwargs and kwargs[field] is not None:
                setattr(shift, field, kwargs[field])

        new_duration = (shift.end_time - shift.start_time).total_seconds() / 3600
        if new_duration <= 0:
            raise ValueError("End time must be after start time.")

        # New times must be in the future
        if shift.start_time <= now:
            raise ValueError("Start time must be in the future.")

        # Cannot reduce quantity below already filled
        if shift.quantity_needed < shift.quantity_filled:
            raise ValueError(
                f"Cannot reduce quantity below {shift.quantity_filled} (already filled)."
            )

        # Re-open if quantity increased and was previously FILLED
        if shift.status == 'FILLED' and shift.quantity_needed > shift.quantity_filled:
            shift.status = 'OPEN'

        # Wallet adjustment: charge or refund the difference
        facility = shift.facility
        old_total = old_rate * Decimal(str(old_duration)) * old_quantity
        new_total = shift.rate * Decimal(str(new_duration)) * shift.quantity_needed
        diff = new_total - old_total

        if diff > 0:
            # Additional charge
            if facility.wallet_balance < diff:
                raise ValueError(
                    f"Insufficient balance for this change. Additional ₦{diff} required, "
                    f"available: ₦{facility.wallet_balance}"
                )
            facility.wallet_balance -= diff
            facility.save(update_fields=['wallet_balance'])
        elif diff < 0:
            # Refund the difference
            facility.wallet_balance += abs(diff)
            facility.save(update_fields=['wallet_balance'])

        shift.save()
        return shift


class ShiftApplyService(BaseService):
    def __call__(self, user, shift_id):
        from django.utils import timezone

        if not user.is_professional:
            raise PermissionError("Only professionals can apply.")

        shift = Shift.objects.get(id=shift_id)
        # Allow applying to OPEN or FILLED shifts (backlog)
        if shift.status not in ('OPEN', 'FILLED'):
            raise ValueError("This shift is no longer accepting applications.")

        # Cannot apply to a shift that has already started
        if shift.start_time <= timezone.now():
            raise ValueError("This shift has already started and is no longer accepting applications.")

        if ShiftApplication.objects.filter(shift=shift, professional=user.professional).exists():
            raise ValueError("Already applied.")

        # Clash Prevention: block if already CONFIRMED/IN_PROGRESS at same time
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
        from django.utils import timezone

        if not user.is_facility:
            raise PermissionError("Only facilities can manage applications.")

        application = ShiftApplication.objects.select_related(
            'shift', 'professional__user'
        ).get(id=application_id)
        if application.shift.facility != user.facility:
            raise PermissionError("Not your shift.")

        if action == 'CONFIRM':
            shift = application.shift

            # Cannot confirm after shift has started
            if shift.start_time <= timezone.now():
                raise ValueError("This shift has already started. You can no longer confirm applicants.")

            if shift.quantity_filled >= shift.quantity_needed:
                raise ValueError("Shift is already filled.")

            # Clash check: ensure this professional doesn't already have a
            # CONFIRMED/IN_PROGRESS shift that overlaps with this one
            clashing_confirmed = ShiftApplication.objects.filter(
                professional=application.professional,
                status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'],
                shift__start_time__lt=shift.end_time,
                shift__end_time__gt=shift.start_time
            ).exclude(id=application.id)

            if clashing_confirmed.exists():
                raise ValueError(
                    "This professional already has a confirmed shift at this time. "
                    "Their application will be removed automatically."
                )

            application.status = 'CONFIRMED'
            application.check_in_code = ShiftApplication.generate_code()
            application.check_out_code = ShiftApplication.generate_code()
            application.save()

            # Update shift filled count
            shift.quantity_filled += 1
            if shift.quantity_filled >= shift.quantity_needed:
                shift.status = 'FILLED'
            shift.save()

            # --- Auto-reject clashing PENDING applications at other shifts ---
            clashing_pending = ShiftApplication.objects.filter(
                professional=application.professional,
                status='PENDING',
                shift__start_time__lt=shift.end_time,
                shift__end_time__gt=shift.start_time
            ).exclude(shift=shift).select_related('shift__facility__user')

            from core.models import Notification

            for clash_app in clashing_pending:
                clash_app.status = 'REJECTED'
                clash_app.save(update_fields=['status', 'updated_at'])

                # Notify the affected facility
                pro_name = (
                    f"{application.professional.user.first_name} "
                    f"{application.professional.user.last_name}".strip()
                    or application.professional.user.email
                )
                Notification.send(
                    user=clash_app.shift.facility.user,
                    title="Applicant No Longer Available",
                    message=(
                        f"{pro_name} has been confirmed for another shift at the same time "
                        f"and is no longer available for '{clash_app.shift.role}'."
                    ),
                    notification_type="CANCELLED",
                    related_object_id=clash_app.id,
                )

            # Notify the professional about confirmation
            Notification.send(
                user=application.professional.user,
                title="Shift Confirmed",
                message=(
                    f"You have been confirmed for '{shift.role}' at {shift.facility.name}. "
                    f"Your check-in code is {application.check_in_code}."
                ),
                notification_type="SHIFT_APPROVED",
                related_object_id=application.id,
            )

            # Send confirmation email
            from .emails import send_shift_confirmed_email
            send_shift_confirmed_email(application)

        elif action == 'REJECT':
            application.status = 'REJECTED'
            application.save()

        return application

class ClockInService(BaseService):
    """Code-based clock-in: professional enters the check-in code given by facility."""
    def __call__(self, user, shift_id, code):
        if not user.is_professional:
            raise PermissionError("Only professionals can clock in.")

        try:
            application = ShiftApplication.objects.get(
                shift__id=shift_id, professional=user.professional, status='CONFIRMED'
            )
        except ShiftApplication.DoesNotExist:
            raise ValueError("No confirmed application for this shift.")

        # Prevent clocking in if already on an active shift
        active_shift = ShiftApplication.objects.filter(
            professional=user.professional,
            status='IN_PROGRESS'
        ).exists()
        if active_shift:
            raise ValueError("You are already clocked into another shift. Please clock out first.")

        # Validate check-in code
        if not application.check_in_code:
            raise ValueError("No check-in code assigned. Contact the facility.")
        if code != application.check_in_code:
            raise ValueError("Invalid check-in code.")

        from django.utils import timezone
        application.clock_in_time = timezone.now()
        application.status = 'IN_PROGRESS'
        application.save()

        # Notify Facility
        from core.models import Notification
        Notification.send(
            user=application.shift.facility.user,
            title="Professional Checked In",
            message=f"{user.first_name or user.email} has checked in for '{application.shift.role}'.",
            notification_type="SHIFT_APPROVED",
            related_object_id=application.id
        )

        return application


class ClockOutService(BaseService):
    """Code-based clock-out: professional enters the check-out code given by facility."""
    def __call__(self, user, shift_id, code):
        if not user.is_professional:
            raise PermissionError("Only professionals can clock out.")

        try:
            application = ShiftApplication.objects.get(
                shift__id=shift_id, professional=user.professional, status='IN_PROGRESS'
            )
        except ShiftApplication.DoesNotExist:
            raise ValueError("No active shift to clock out of.")

        # Validate check-out code
        if not application.check_out_code:
            raise ValueError("No check-out code assigned. Contact the facility.")
        if code != application.check_out_code:
            raise ValueError("Invalid check-out code.")

        from django.utils import timezone
        application.clock_out_time = timezone.now()
        application.status = 'COMPLETED'
        application.save()

        # Trigger Payment
        from billing.tasks import payout_professional
        payout_professional.apply_async((application.id,), countdown=24*3600)

        # Notify Facility
        from core.models import Notification
        Notification.send(
            user=application.shift.facility.user,
            title="Professional Checked Out",
            message=f"{user.first_name or user.email} has checked out of '{application.shift.role}'.",
            notification_type="SHIFT_APPROVED",
            related_object_id=application.id
        )

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
        Notification.send(
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
        Notification.send(
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
        Notification.send(
            user=request.shift_application.professional.user,
            title="Extra Time Approved",
            message=f"Your request for {request.hours}hrs extra time has been approved.",
            notification_type="SHIFT_APPROVED",
            data={"request_id": str(request.id)}
        )
        
        return request

