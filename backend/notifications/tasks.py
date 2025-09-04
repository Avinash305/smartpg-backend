from __future__ import annotations
import logging
from django.conf import settings
from django.core.mail import send_mail
from celery import shared_task
from django.utils import timezone

from .models import Notification

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_email_notification(self, notification_id: int):
    try:
        n = Notification.objects.select_related("recipient").get(pk=notification_id)
    except Notification.DoesNotExist:
        return

    if not n.recipient.email:
        logger.info("Notification %s recipient has no email; skipping", n.id)
        return

    subject = n.title or f"Notification: {n.event}"
    message = n.message or (n.payload.get("body") if isinstance(n.payload, dict) else "") or ""
    from_email = getattr(settings, "NOTIFICATIONS_EMAIL_FROM", getattr(settings, "DEFAULT_FROM_EMAIL", None))
    if not from_email:
        logger.warning("Email FROM not configured; set NOTIFICATIONS_EMAIL_FROM or DEFAULT_FROM_EMAIL")
        return

    try:
        send_mail(subject, message, from_email, [n.recipient.email], fail_silently=False)
        logger.info("Email sent for notification %s", n.id)
    except Exception as e:
        logger.exception("Failed to send email for notification %s: %s", n.id, e)
        raise


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_sms_notification(self, notification_id: int):
    try:
        n = Notification.objects.select_related("recipient").get(pk=notification_id)
    except Notification.DoesNotExist:
        return

    phone = getattr(n.recipient, "phone", None)
    if not phone:
        logger.info("Notification %s recipient has no phone; skipping SMS", n.id)
        return

    body = n.message or (n.payload.get("body") if isinstance(n.payload, dict) else "") or n.title or n.event

    # Twilio example (optional)
    account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", None)
    auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", None)
    from_phone = getattr(settings, "TWILIO_SMS_FROM", None)

    if not (account_sid and auth_token and from_phone):
        logger.warning("Twilio SMS not configured; set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_SMS_FROM")
        return

    try:
        from twilio.rest import Client  # type: ignore
        client = Client(account_sid, auth_token)
        client.messages.create(body=body, from_=from_phone, to=f"+91{phone}" if len(phone) == 10 else phone)
        logger.info("SMS sent for notification %s", n.id)
    except Exception as e:
        logger.exception("Failed to send SMS for notification %s: %s", n.id, e)
        raise
