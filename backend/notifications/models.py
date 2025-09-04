from django.db import models
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.utils import timezone


class Notification(models.Model):
    LEVEL_CHOICES = (
        ("info", "Info"),
        ("success", "Success"),
        ("warning", "Warning"),
        ("error", "Error"),
    )

    # Who did the action (optional)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notifications_sent",
        help_text="User that performed the action (if any)",
    )

    # Who receives this notification (required for in-app delivery)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        help_text="User that should see this notification",
    )

    # Short verb/event key: e.g., bookings.created, payments.received
    event = models.CharField(max_length=100, db_index=True)

    # Optional human text
    title = models.CharField(max_length=200, blank=True)
    message = models.TextField(blank=True)

    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default="info")

    # Generic relation to any subject object
    subject_content_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.SET_NULL)
    subject_object_id = models.CharField(max_length=64, null=True, blank=True)
    subject = GenericForeignKey("subject_content_type", "subject_object_id")

    # Scoping fields to support RBAC filtering (pg_admin org and building scope)
    pg_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scoped_notifications",
        help_text="Owning PG Admin for scoping",
        limit_choices_to={"role": "pg_admin"},
    )
    # Building is optional; when present, used for pg_staff per-building filtering
    building = models.ForeignKey(
        "properties.Building",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notifications",
    )

    # Delivery and extra data
    channels = models.JSONField(default=list, blank=True, help_text='e.g., ["in_app", "email"]')
    payload = models.JSONField(default=dict, blank=True)

    # Read state
    unread = models.BooleanField(default=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    # Audit
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "unread", "-created_at"], name="notif_rec_unread_idx"),
            models.Index(fields=["pg_admin", "building", "-created_at"], name="notif_scope_idx"),
            models.Index(fields=["event", "-created_at"], name="notif_event_idx"),
        ]

    def mark_read(self, *, save: bool = True):
        if self.unread:
            self.unread = False
            self.read_at = timezone.now()
            if save:
                self.save(update_fields=["unread", "read_at"])

    def __str__(self) -> str:
        return f"{self.event} -> {self.recipient_id} ({'unread' if self.unread else 'read'})"

# The former NotificationSettings model (WhatsApp-related) has been removed.
