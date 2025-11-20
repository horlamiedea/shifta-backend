from core.services import BaseSelector
from .models import User

class UserSelector(BaseSelector):
    def get_user_by_email(self, email):
        try:
            return User.objects.get(email=email)
        except User.DoesNotExist:
            return None

    def get_profile_data(self, user):
        data = {
            "id": str(user.id),
            "email": user.email,
            "is_professional": user.is_professional,
            "is_facility": user.is_facility,
        }
        if user.is_professional:
            data["professional"] = {
                "license_number": user.professional.license_number,
                "specialties": user.professional.specialties,
                "is_verified": user.professional.is_verified
            }
        if user.is_facility:
            data["facility"] = {
                "name": user.facility.name,
                "rc_number": user.facility.rc_number,
                "is_verified": user.facility.is_verified
            }
        return data
