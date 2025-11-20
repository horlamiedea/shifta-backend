from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _
from .managers import CustomUserManager
from core.models import BaseModel
import uuid

class User(AbstractUser):
    username = None
    email = models.EmailField(_('email address'), unique=True)
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    def __str__(self):
        return self.email

    @property
    def is_professional(self):
        return hasattr(self, 'professional')

    @property
    def is_facility(self):
        return hasattr(self, 'facility')


class Professional(BaseModel):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='professional')
    license_number = models.CharField(max_length=50, unique=True)
    license_expiry_date = models.DateField(null=True, blank=True)
    specialties = models.JSONField(default=list)  # e.g. ["ICU", "Pediatrics"]
    cv_url = models.URLField(null=True, blank=True)
    certificate_url = models.URLField(null=True, blank=True)
    is_verified = models.BooleanField(default=False)
    rejection_reason = models.TextField(null=True, blank=True)
    current_location_lat = models.FloatField(null=True, blank=True)
    current_location_lng = models.FloatField(null=True, blank=True)
    
    # Phase 2: Wallet & Multi-Currency
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    country = models.CharField(max_length=100, default='Nigeria')
    currency = models.CharField(max_length=10, default='NGN')

    def __str__(self):
        return f"Professional: {self.user.email}"


class Facility(BaseModel):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='facility')
    name = models.CharField(max_length=255)
    address = models.TextField()
    rc_number = models.CharField(max_length=50, unique=True)
    # Renamed credit_balance to wallet_balance for consistency with prepaid model
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00) # Can be used for overdraft
    tier = models.IntegerField(default=1)  # 1-4
    is_verified = models.BooleanField(default=False)
    location_lat = models.FloatField(null=True, blank=True)
    location_lng = models.FloatField(null=True, blank=True)
    
    # Phase 2: Wallet & Multi-Currency
    country = models.CharField(max_length=100, default='Nigeria')
    currency = models.CharField(max_length=10, default='NGN')

    def __str__(self):
        return self.name

class Review(BaseModel):
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews_given')
    target_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews_received')
    rating = models.IntegerField() # 1-5
    comment = models.TextField()
    
    def __str__(self):
        return f"{self.rating} star review for {self.target_user}"

class WaitlistProfessional(BaseModel):
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=20)
    medical_type = models.CharField(max_length=100) # e.g. Nurse, Doctor, etc.
    bio_data = models.TextField(null=True, blank=True) # Any other details
    
    def __str__(self):
        return f"Waitlist: {self.email} ({self.medical_type})"
