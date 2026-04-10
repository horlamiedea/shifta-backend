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
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone_number": user.phone_number,
            "is_professional": user.is_professional,
            "is_facility": user.is_facility,
        }
        if user.is_professional:
            data["professional"] = {
                "license_number": user.professional.license_number,
                "specialties": user.professional.specialties,
                "is_verified": user.professional.is_verified,
            }
        if user.is_facility:
            data["facility"] = {
                "name": user.facility.name,
                "address": user.facility.address,
                "rc_number": user.facility.rc_number,
                "is_verified": user.facility.is_verified,
                "wallet_balance": str(user.facility.wallet_balance),
            }
        return data
