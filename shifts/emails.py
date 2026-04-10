from django.core.mail import send_mail
from django.conf import settings


def send_shift_promotion_email(application):
    """
    Send an email to a professional who has been auto-promoted from the
    backlog to a confirmed shift slot.
    """
    professional = application.professional
    shift = application.shift
    user = professional.user

    pro_name = (
        f"{user.first_name} {user.last_name}".strip() or user.email
    )
    facility_name = shift.facility.name
    shift_date = shift.start_time.strftime("%B %d, %Y")
    shift_time = f"{shift.start_time.strftime('%I:%M %p')} – {shift.end_time.strftime('%I:%M %p')}"

    subject = f"You've been confirmed for {shift.role} at {facility_name}!"
    message = (
        f"Hi {pro_name},\n\n"
        f"Great news! A spot opened up and you've been confirmed for the following shift:\n\n"
        f"  Role: {shift.role} ({shift.specialty})\n"
        f"  Facility: {facility_name}\n"
        f"  Date: {shift_date}\n"
        f"  Time: {shift_time}\n"
        f"  Rate: ₦{shift.rate:,.2f}/hr\n\n"
        f"  Your Check-In Code: {application.check_in_code}\n"
        f"  Your Check-Out Code: {application.check_out_code}\n\n"
        f"Please arrive on time and use your check-in code when you get to the facility.\n\n"
        f"— The Shifta Team"
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception:
        # Email failures should not block the promotion
        pass


def send_shift_confirmed_email(application):
    """
    Send an email to a professional when they are confirmed for a shift.
    """
    professional = application.professional
    shift = application.shift
    user = professional.user

    pro_name = (
        f"{user.first_name} {user.last_name}".strip() or user.email
    )
    facility_name = shift.facility.name
    shift_date = shift.start_time.strftime("%B %d, %Y")
    shift_time = f"{shift.start_time.strftime('%I:%M %p')} – {shift.end_time.strftime('%I:%M %p')}"

    subject = f"Shift Confirmed: {shift.role} at {facility_name}"
    message = (
        f"Hi {pro_name},\n\n"
        f"You have been confirmed for the following shift:\n\n"
        f"  Role: {shift.role} ({shift.specialty})\n"
        f"  Facility: {facility_name}\n"
        f"  Date: {shift_date}\n"
        f"  Time: {shift_time}\n"
        f"  Rate: ₦{shift.rate:,.2f}/hr\n\n"
        f"  Your Check-In Code: {application.check_in_code}\n"
        f"  Your Check-Out Code: {application.check_out_code}\n\n"
        f"Please arrive on time and use your check-in code when you get to the facility.\n\n"
        f"— The Shifta Team"
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception:
        pass
