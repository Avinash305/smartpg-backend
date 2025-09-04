from __future__ import annotations
from typing import Iterable, Sequence
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from django.contrib.auth import get_user_model

from .models import Notification
from .tasks import (
    send_email_notification,
    send_sms_notification,
)

User = get_user_model()

# ---- Event registry: defaults per event ----
# title/message can use str.format() with keys from payload and common fields
EVENT_TEMPLATES: dict[str, dict] = {
    "booking.created": {
        "title": "New booking created",
        "message": "Booking for {tenant} at {building} starting {start_date}",
        "channels": ["in_app"],
        "level": "info",
    },
    "booking.status_changed": {
        "title": "Booking status updated",
        "message": "Status changed from {old_status} to {status} for {tenant}",
        "channels": ["in_app"],
        "level": "info",
    },
    "payment.success": {
        "title": "Payment received",
        "message": "₹{amount} received for booking {booking_id} ({tenant})",
        "channels": ["in_app"],
        "level": "success",
    },
    "payment.failed": {
        "title": "Payment failed",
        "message": "Payment attempt for booking {booking_id} ({tenant}) failed via {method}. Ref: {reference}",
        "channels": ["in_app"],
        "level": "warning",
    },
    "payment.refunded": {
        "title": "Payment refunded",
        "message": "₹{amount} refunded for booking {booking_id} ({tenant}). Ref: {reference}",
        "channels": ["in_app"],
        "level": "info",
    },
    # Properties
    "building.created": {
        "title": "Building added",
        "message": "{building} • {city}, {state} ({property_type})",
        "channels": ["in_app"],
        "level": "info",
    },
    "building.updated": {
        "title": "Building updated",
        "message": "{building} • {city}, {state}",
        "channels": ["in_app"],
        "level": "info",
    },
    "building.deleted": {
        "title": "Building deleted",
        "message": "{building} removed ({city}, {state})",
        "channels": ["in_app"],
        "level": "warning",
    },
    "floor.created": {
        "title": "Floor added",
        "message": "{building} • {floor}",
        "channels": ["in_app"],
        "level": "info",
    },
    "floor.updated": {
        "title": "Floor updated",
        "message": "{building} • {floor}",
        "channels": ["in_app"],
        "level": "info",
    },
    "floor.deleted": {
        "title": "Floor deleted",
        "message": "{building} • {floor}",
        "channels": ["in_app"],
        "level": "warning",
    },
    "room.created": {
        "title": "Room added",
        "message": "{building} • {floor} • Room {room}",
        "channels": ["in_app"],
        "level": "info",
    },
    "room.updated": {
        "title": "Room updated",
        "message": "{building} • {floor} • Room {room}",
        "channels": ["in_app"],
        "level": "info",
    },
    "room.deleted": {
        "title": "Room deleted",
        "message": "{building} • {floor} • Room {room}",
        "channels": ["in_app"],
        "level": "warning",
    },
    "bed.created": {
        "title": "Bed added",
        "message": "{building} • {floor} • Room {room} • Bed {bed}",
        "channels": ["in_app"],
        "level": "info",
    },
    "bed.status_changed": {
        "title": "Bed status updated",
        "message": "{building} • Room {room} • Bed {bed}: {old_status} → {status}",
        "channels": ["in_app"],
        "level": "info",
    },
    "bed.updated": {
        "title": "Bed updated",
        "message": "{building} • {floor} • Room {room} • Bed {bed}",
        "channels": ["in_app"],
        "level": "info",
    },
    "bed.deleted": {
        "title": "Bed deleted",
        "message": "{building} • {floor} • Room {room} • Bed {bed}",
        "channels": ["in_app"],
        "level": "warning",
    },
    # Tenants
    "tenant.created": {
        "title": "Tenant added",
        "message": "{tenant} • {phone} • {building}",
        "channels": ["in_app"],
        "level": "info",
    },
    "tenant.moved": {
        "title": "Tenant moved",
        "message": "{tenant}: {old_building} → {new_building}",
        "channels": ["in_app"],
        "level": "info",
    },
    "tenant.status_changed": {
        "title": "Tenant status updated",
        "message": "{tenant}: {old_is_active} → {new_is_active}",
        "channels": ["in_app"],
        "level": "info",
    },
    # Rent/Billing reminders
    "rent.due_soon": {
        "title": "Rent due soon",
        "message": "{tenant} • {building} • Due on {due_on} • Amount: ₹{amount}",
        "channels": ["in_app"],
        "level": "info",
    },
    "rent.due_today": {
        "title": "Rent due today",
        "message": "{tenant} • {building} • Due today • Amount: ₹{amount}",
        "channels": ["in_app"],
        "level": "warning",
    },
    "rent.overdue": {
        "title": "Rent overdue",
        "message": "{tenant} • {building} • Overdue since {due_on} • Amount: ₹{amount}",
        "channels": ["in_app"],
        "level": "warning",
    },
}


def _apply_event_defaults(event: str, title: str, message: str, level: str, channels: Sequence[str] | None, payload: dict | None):
    tpl = EVENT_TEMPLATES.get(event) or {}
    # Defaults
    if not channels:
        channels = tpl.get("channels") or ["in_app"]
    if not level:
        level = tpl.get("level") or "info"
    # Interpolate using payload keys if available
    fmt_payload = payload or {}
    try:
        if not title:
            title = str(tpl.get("title") or "").format(**fmt_payload)
    except Exception:
        title = tpl.get("title") or title or ""
    try:
        if not message:
            message = str(tpl.get("message") or "").format(**fmt_payload)
    except Exception:
        message = tpl.get("message") or message or ""
    return title, message, level, list(channels)


def notify(
    *,
    event: str,
    recipient: User | int | Sequence[User | int],
    actor: User | int | None = None,
    title: str = "",
    message: str = "",
    level: str = "info",
    subject: object | None = None,
    pg_admin: User | int | None = None,
    building: object | int | None = None,
    payload: dict | None = None,
    channels: Sequence[str] | None = None,
) -> list[Notification]:
    """
    Create in-app notification(s). `recipient` can be a user, user id, or a list of them.
    `subject` can be any model instance; we will store a GenericForeignKey.
    `pg_admin` is recommended for scoping; pass the owning admin (user instance or id).
    `building` can be a `properties.Building` instance or id and is used for RBAC scoping for staff.

    Delivery:
    - Default channels = ["in_app"], or from EVENT_TEMPLATES if configured.
    - If channels contain "email"/"sms", Celery tasks will be enqueued after commit.
    """
    # Allow registry to provide sensible defaults
    title, message, level, channels = _apply_event_defaults(event, title, message, level, channels, payload or {})

    # normalize channel names
    channels = [str(ch).lower() for ch in (channels or ["in_app"])]

    recipients: list[User | int]
    if isinstance(recipient, (list, tuple, set)):
        recipients = list(recipient)
    else:
        recipients = [recipient]

    subject_ct = None
    subject_id = None
    if subject is not None:
        subject_ct = ContentType.objects.get_for_model(subject.__class__)
        # tolerant cast to str for object_id to allow UUID or int
        subject_id = str(getattr(subject, "pk", getattr(subject, "id", None)))

    building_id = None
    if building is not None:
        building_id = getattr(building, "pk", getattr(building, "id", building))

    pg_admin_id = None
    if pg_admin is not None:
        pg_admin_id = getattr(pg_admin, "pk", getattr(pg_admin, "id", pg_admin))

    actor_id = None
    if actor is not None:
        actor_id = getattr(actor, "pk", getattr(actor, "id", actor))

    notifications: list[Notification] = []
    created_ids: list[int] = []

    def _enqueue_tasks(nid: int):
        if "email" in channels:
            send_email_notification.delay(nid)
        if "sms" in channels:
            send_sms_notification.delay(nid)

    with transaction.atomic():
        for r in recipients:
            recipient_id = getattr(r, "pk", getattr(r, "id", r))
            n = Notification.objects.create(
                actor_id=actor_id,
                recipient_id=recipient_id,
                event=event,
                title=title or "",
                message=message or "",
                level=level,
                subject_content_type=subject_ct,
                subject_object_id=subject_id,
                pg_admin_id=pg_admin_id,
                building_id=building_id,
                payload=payload or {},
                channels=list(channels),
                unread=True,
                created_at=timezone.now(),
            )
            notifications.append(n)
            created_ids.append(n.id)

        # Enqueue deliveries after the transaction commits
        for nid in created_ids:
            transaction.on_commit(lambda nid=nid: _enqueue_tasks(nid))

    return notifications
