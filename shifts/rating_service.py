"""
Rating & Review Service

Handles:
  - Facility submitting a rating for a professional after a completed shift
  - Auto-generated ratings from cancellation penalties
  - Refreshing a professional's cached score fields
"""

from django.db import transaction, models
from core.services import BaseService
from accounts.models import Review
from .models import ShiftApplication


class RatingService(BaseService):
    @transaction.atomic
    def submit_review(self, user, application_id, rating, comment=''):
        """
        Facility rates a professional after a COMPLETED shift.
        One review per application. Rating 1–5, comment optional.
        """
        if not user.is_facility:
            raise PermissionError("Only facilities can rate professionals.")

        application = ShiftApplication.objects.select_related(
            'shift__facility', 'professional__user'
        ).get(id=application_id)

        if application.shift.facility != user.facility:
            raise PermissionError("Not your shift.")

        if application.status != 'COMPLETED':
            raise ValueError("Can only rate professionals after a completed shift.")

        if hasattr(application, 'review') and application.review is not None:
            raise ValueError("This professional has already been rated for this shift.")

        if not (1 <= rating <= 5):
            raise ValueError("Rating must be between 1 and 5.")

        review = Review.objects.create(
            shift_application=application,
            reviewer=user,
            target_user=application.professional.user,
            rating=rating,
            comment=comment,
            is_auto=False,
        )

        # Refresh the professional's cached stats
        refresh_professional_stats(application.professional)

        return review

    @staticmethod
    def create_auto_review(application, rating, comment, reviewer_user):
        """
        Create a system-generated review (e.g. cancellation penalty).
        Does not raise if a review already exists — cancellation reviews
        are stored even if a manual review exists (they use different
        shift_applications or no application link).
        """
        review = Review.objects.create(
            shift_application=application,
            reviewer=reviewer_user,
            target_user=application.professional.user,
            rating=rating,
            comment=comment,
            is_auto=True,
        )

        refresh_professional_stats(application.professional)
        return review


def refresh_professional_stats(professional):
    """
    Recompute and save the professional's cached rating fields from
    all Reviews targeting them and all their ShiftApplications.
    """
    from django.db.models import Avg, Count

    stats = Review.objects.filter(
        target_user=professional.user
    ).aggregate(
        avg=Avg('rating'),
        count=Count('id'),
    )

    professional.avg_rating = stats['avg'] or 0
    professional.total_ratings = stats['count'] or 0

    # Count completed and cancelled applications
    app_stats = ShiftApplication.objects.filter(
        professional=professional
    ).aggregate(
        completed=Count('id', filter=models.Q(status='COMPLETED')),
        cancelled=Count('id', filter=models.Q(status='CANCELLED', cancelled_by='PROFESSIONAL')),
    )

    professional.total_completed_shifts = app_stats['completed'] or 0
    professional.total_cancelled_shifts = app_stats['cancelled'] or 0

    professional.save(update_fields=[
        'avg_rating', 'total_ratings',
        'total_completed_shifts', 'total_cancelled_shifts',
        'updated_at',
    ])
