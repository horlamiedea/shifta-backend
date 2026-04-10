from celery import shared_task
from django.utils import timezone
from accounts.models import Professional
from core.utils import haversine
from core.models import Notification
from .models import Shift, ShiftApplication


@shared_task
def notify_matching_professionals(shift_id):
    """
    Notify professionals about a new shift, prioritised by professional_score.
    Higher-scored professionals receive the notification first.
    """
    try:
        shift = Shift.objects.select_related('facility').get(id=shift_id)
    except Shift.DoesNotExist:
        return

    target_lat = shift.latitude or shift.facility.location_lat
    target_lng = shift.longitude or shift.facility.location_lng

    if not target_lat or not target_lng:
        return

    potential_candidates = Professional.objects.filter(
        specialties__contains=[shift.specialty],
        is_verified=True,
        current_location_lat__isnull=False,
        current_location_lng__isnull=False,
    ).select_related('user')

    matching_pros = []
    for pro in potential_candidates:
        dist = haversine(
            pro.current_location_lat, pro.current_location_lng,
            target_lat, target_lng,
        )
        if dist > 20:
            continue

        has_clash = ShiftApplication.objects.filter(
            professional=pro,
            status__in=['CONFIRMED', 'IN_PROGRESS', 'ATTENDANCE_PENDING'],
            shift__start_time__lt=shift.end_time,
            shift__end_time__gt=shift.start_time,
        ).exists()
        if has_clash:
            continue

        matching_pros.append(pro)

    # Sort by professional_score descending — top-rated get alerted first
    matching_pros.sort(key=lambda p: p.professional_score, reverse=True)

    for pro in matching_pros:
        Notification.objects.create(
            user=pro.user,
            title="New Shift Available",
            message=(
                f"A new {shift.role} ({shift.specialty}) shift is available at "
                f"{shift.facility.name}. ₦{shift.rate:,}/hr."
            ),
            notification_type="SHIFT_POSTED",
            related_object_id=shift.id,
            data={"shift_id": str(shift.id)},
        )
        # TODO: Send push notification / SMS when integrated
        print(f"Notified {pro.user.email} (score={pro.professional_score})")


@shared_task
def close_expired_shifts():
    """
    Runs hourly. Handles two cases:

    1. OPEN/FILLED shifts whose end_time has passed → mark COMPLETED,
       refund unfilled spots, reject remaining PENDING apps.
    2. IN_PROGRESS applications whose shift end_time has passed by 2+ hours
       and the professional forgot to clock out → auto-complete them.
    """
    from decimal import Decimal
    from .rating_service import refresh_professional_stats
    from billing.tasks import payout_professional

    now = timezone.now()

    # --- 1. Expire OPEN/FILLED shifts past their end time ---
    expired_shifts = Shift.objects.filter(
        status__in=['OPEN', 'FILLED'],
        end_time__lte=now,
    )

    shift_count = 0
    for shift in expired_shifts:
        shift.status = 'COMPLETED'
        shift.save(update_fields=['status', 'updated_at'])

        # Refund for unfilled spots
        unfilled = shift.quantity_needed - shift.quantity_filled
        if unfilled > 0:
            duration_hours = (shift.end_time - shift.start_time).total_seconds() / 3600
            refund = shift.rate * Decimal(str(duration_hours)) * unfilled
            facility = shift.facility
            facility.wallet_balance += refund
            facility.save(update_fields=['wallet_balance'])

        # Reject remaining PENDING applications
        ShiftApplication.objects.filter(
            shift=shift, status='PENDING',
        ).update(status='REJECTED')

        shift_count += 1

    # --- 2. Auto-complete forgotten clock-outs (2 h grace period) ---
    grace = now - timezone.timedelta(hours=2)
    stuck_apps = ShiftApplication.objects.filter(
        status='IN_PROGRESS',
        shift__end_time__lte=grace,
        clock_out_time__isnull=True,
    ).select_related('shift__facility__user', 'professional')

    auto_count = 0
    for app in stuck_apps:
        app.clock_out_time = app.shift.end_time  # Clock out at scheduled end
        app.status = 'COMPLETED'
        app.save(update_fields=['status', 'clock_out_time', 'updated_at'])

        # Trigger payout (immediate since shift already ended)
        payout_professional.apply_async((app.id,), countdown=60)

        refresh_professional_stats(app.professional)

        Notification.objects.create(
            user=app.professional.user,
            title="Shift Auto-Completed",
            message=(
                f"Your shift '{app.shift.role}' was automatically completed "
                f"because you did not clock out. Payment is being processed."
            ),
            notification_type="SHIFT_APPROVED",
            related_object_id=app.id,
        )
        auto_count += 1

    return f"Completed {shift_count} expired shifts, auto-completed {auto_count} applications."

