from django.contrib.auth import authenticate
from rest_framework.authtoken.models import Token
from django.db import transaction
from core.services import BaseService
from .models import User, Professional, Facility

class UserRegisterService(BaseService):
    @transaction.atomic
    def __call__(self, email, password, user_type, **kwargs):
        if User.objects.filter(email=email).exists():
            raise ValueError("User with this email already exists.")
        
        user = User.objects.create_user(email=email, password=password)
        
        if user_type == "professional":
            Professional.objects.create(user=user, **kwargs)
        elif user_type == "facility":
            Facility.objects.create(user=user, **kwargs)
        else:
            # Admin or other types
            pass
            
        token, _ = Token.objects.get_or_create(user=user)
        return user, token.key

class UserLoginService(BaseService):
    def __call__(self, email, password):
        user = authenticate(email=email, password=password)
        if not user:
            raise ValueError("Invalid credentials.")
        
        token, _ = Token.objects.get_or_create(user=user)
        return user, token.key

class AdminVerifyFacilityService(BaseService):
    def __call__(self, facility_id, tier, credit_limit, admin_user):
        if not admin_user.is_staff:
            raise PermissionError("Only admins can verify facilities.")
        
        facility = Facility.objects.get(id=facility_id)
        facility.is_verified = True
        facility.tier = tier
        facility.credit_limit = credit_limit
        facility.save()
        
        return facility

class AdminVerifyProfessionalService(BaseService):
    def __call__(self, professional_id, admin_user):
        if not admin_user.is_staff:
            raise PermissionError("Only admins can verify professionals.")
            
        professional = Professional.objects.get(id=professional_id)
        professional.is_verified = True
        professional.save()
        
        professional.save()
        
        return professional

class ProfessionalUpdateService(BaseService):
    def __call__(self, user, specialties=None, location_lat=None, location_lng=None, cv_url=None, certificate_url=None):
        if not user.is_professional:
            raise ValueError("User is not a professional.")
            
        professional = user.professional
        
        if specialties is not None:
            professional.specialties = specialties
        if location_lat is not None:
            professional.current_location_lat = location_lat
        if location_lng is not None:
            professional.current_location_lng = location_lng
        if cv_url is not None:
            professional.cv_url = cv_url
            
        if certificate_url is not None:
            professional.certificate_url = certificate_url
            # Trigger AI Verification
            from .tasks import verify_professional_certificate
            verify_professional_certificate.delay(professional.id)
            
        professional.save()
        professional.save()
        return professional

from .models import FacilityStaff

class FacilityStaffService(BaseService):
    @transaction.atomic
    def create_staff(self, facility, email, password, role, permissions):
        # Check if user exists
        user = User.objects.filter(email=email).first()
        if not user:
            user = User.objects.create_user(email=email, password=password)
        
        # Check if already staff
        if FacilityStaff.objects.filter(facility=facility, user=user).exists():
            raise ValueError("User is already a staff member of this facility.")
            
        staff = FacilityStaff.objects.create(
            facility=facility,
            user=user,
            role=role,
            can_create_shifts=permissions.get('can_create_shifts', False),
            can_manage_staff=permissions.get('can_manage_staff', False),
            can_view_financials=permissions.get('can_view_financials', False)
        )
        return staff

    def update_staff(self, staff, role=None, permissions=None):
        if role:
            staff.role = role
        if permissions:
            staff.can_create_shifts = permissions.get('can_create_shifts', staff.can_create_shifts)
            staff.can_manage_staff = permissions.get('can_manage_staff', staff.can_manage_staff)
            staff.can_view_financials = permissions.get('can_view_financials', staff.can_view_financials)
        staff.save()
        return staff
