from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator, MaxValueValidator
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

    # Cached rating & reliability stats (updated by RatingService)
    avg_rating = models.DecimalField(max_digits=3, decimal_places=2, default=0.00)
    total_ratings = models.PositiveIntegerField(default=0)
    total_completed_shifts = models.PositiveIntegerField(default=0)
    total_cancelled_shifts = models.PositiveIntegerField(default=0)

    @property
    def completion_rate(self):
        total = self.total_completed_shifts + self.total_cancelled_shifts
        if total == 0:
            return 1.0  # New professionals get benefit of the doubt
        return self.total_completed_shifts / total

    @property
    def professional_score(self):
        """
        Composite score (0–5) used for shift notification priority & ranking.

        Formula:
          score = (avg_rating × 0.50)          — quality of work
                + (completion_rate × 5 × 0.35)  — reliability
                + (newbie_bonus × 0.15)          — give newcomers a fair chance

        Newcomers (< 3 ratings) get a 4.0 baseline so they aren't buried.
        """
        if self.total_ratings < 3:
            # Not enough data — use a generous baseline
            return 4.0

        rating_component = float(self.avg_rating) * 0.50
        reliability_component = self.completion_rate * 5 * 0.35
        # Small bonus that decays as more ratings come in (0 after 20 ratings)
        newbie_bonus = max(0, (20 - self.total_ratings) / 20) * 5 * 0.15

        return round(rating_component + reliability_component + newbie_bonus, 2)

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
    
    # Verification Documents
    cac_file = models.FileField(upload_to='facility/cac/', null=True, blank=True)
    license_file = models.FileField(upload_to='facility/licenses/', null=True, blank=True)
    other_documents = models.FileField(upload_to='facility/others/', null=True, blank=True)
    
    # Phase 2: Wallet & Multi-Currency
    country = models.CharField(max_length=100, default='Nigeria')
    currency = models.CharField(max_length=10, default='NGN')

    def __str__(self):
        return self.name

class FacilityStaff(BaseModel):
    ROLE_CHOICES = (
        ('ADMIN', 'Admin'),
        ('MANAGER', 'Manager'),
        ('STAFF', 'Staff'),
    )
    
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='facility_staff_profile')
    facility = models.ForeignKey(Facility, on_delete=models.CASCADE, related_name='staff_members')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='STAFF')
    # Simple permissions for now, can be expanded to JSONField later
    can_create_shifts = models.BooleanField(default=False)
    can_manage_staff = models.BooleanField(default=False)
    can_view_financials = models.BooleanField(default=False)
    
    def __str__(self):
        return f"{self.user.email} - {self.role} at {self.facility.name}"

class Review(BaseModel):
    shift_application = models.OneToOneField(
        'shifts.ShiftApplication', on_delete=models.CASCADE,
        related_name='review', null=True, blank=True,
    )
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews_given')
    target_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews_received')
    rating = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True, default='')
    is_auto = models.BooleanField(default=False)  # True for cancellation penalties

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['shift_application'],
                condition=models.Q(shift_application__isnull=False),
                name='unique_review_per_application',
            ),
        ]

    def __str__(self):
        return f"{self.rating}★ for {self.target_user}"

class WaitlistProfessional(BaseModel):
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=20)
    medical_type = models.CharField(max_length=100) # e.g. Nurse, Doctor, etc.
    cv_file = models.FileField(upload_to='waitlist/cvs/', null=True, blank=True)
    license_file = models.FileField(upload_to='waitlist/licenses/', null=True, blank=True)
    location = models.CharField(max_length=255, null=True, blank=True)
    preferred_work_address = models.TextField(null=True, blank=True)
    shift_rate_9hr = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    shift_rate_12hr = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    shift_rate_16hr = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    shift_rate_24hr = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    years_of_experience = models.IntegerField(null=True, blank=True)
    bio_data = models.TextField(null=True, blank=True) # Any other details
    
    def __str__(self):
        return f"Waitlist: {self.email} ({self.medical_type})"
