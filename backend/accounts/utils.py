import random
from datetime import timedelta
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


def generate_numeric_otp(length: int = 6) -> str:
    """Generate a zero-padded numeric OTP of given length."""
    max_num = 10 ** length - 1
    return f"{random.randint(0, max_num):0{length}d}"


def set_email_otp_for(obj, ttl_minutes: int = 10) -> str:
    """Assign a fresh OTP to obj (User or PendingRegistration) and persist."""
    code = generate_numeric_otp(6)
    obj.email_otp = code
    obj.email_otp_expires_at = timezone.now() + timedelta(minutes=ttl_minutes)
    obj.email_otp_last_sent_at = timezone.now()
    obj.email_otp_attempts = 0
    # For User, also ensure not verified yet when sending a fresh OTP
    if hasattr(obj, 'email_verified'):
        setattr(obj, 'email_verified', False)
    obj.save(update_fields=[
        'email_otp', 'email_otp_expires_at', 'email_otp_last_sent_at', 'email_otp_attempts'
    ] + (['email_verified'] if hasattr(obj, 'email_verified') else []))
    return code


def set_user_email_otp(user, ttl_minutes: int = 10) -> str:
    """Assign a fresh OTP to user with expiry and update last sent timestamp."""
    code = generate_numeric_otp(6)
    user.email_otp = code
    user.email_otp_expires_at = timezone.now() + timedelta(minutes=ttl_minutes)
    user.email_otp_last_sent_at = timezone.now()
    user.email_otp_attempts = 0
    user.email_verified = False
    user.save(update_fields=[
        'email_otp', 'email_otp_expires_at', 'email_otp_last_sent_at', 'email_otp_attempts', 'email_verified'
    ])
    return code


def send_email_otp(user, code: str):
    """Send the OTP to user's email address."""
    subject = "Your PG Management System email verification code"
    message = (
        f"Hello {getattr(user, 'full_name', '') or ''}\n\n"
        f"Your verification code is: {code}\n\n"
        f"This code will expire in 10 minutes.\n\n"
        f"If you did not request this, please ignore this email."
    )
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@example.com')
    try:
        send_mail(subject, message, from_email, [getattr(user, 'email')], fail_silently=False)
    except Exception as e:
        # Log full exception so SMTP issues are visible during debugging
        logger.exception("Failed to send OTP email to %s: %s", getattr(user, 'email', 'unknown'), str(e))
        raise
