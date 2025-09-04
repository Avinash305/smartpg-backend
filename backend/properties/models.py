from django.db import models
from django.conf import settings
from django.core.validators import RegexValidator, MinValueValidator
from django.core.exceptions import ValidationError
from accounts.middleware import get_current_user
from django.db.models.signals import post_save, pre_save, post_delete
from django.dispatch import receiver

# Choices for Building.property_type
PROPERTY_TYPE_CHOICES = (
    ("boys", "Boys"),
    ("girls", "Girls"),
    ("coliving", "Co-Living"),
)

# Floors list: Ground Floor (0) to 14th Floor
_DEF_FLOOR_MAX = 14

def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

FLOOR_CHOICES = tuple(
    [(0, "Ground Floor")] + [(i, f"{_ordinal(i)} Floor") for i in range(1, _DEF_FLOOR_MAX + 1)]
)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="%(app_label)s_%(class)s_created",
        help_text="User who created this record",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="%(app_label)s_%(class)s_updated",
        help_text="User who last updated this record",
    )

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        user = get_current_user()
        if user and user.is_authenticated:
            if not self.pk and not self.created_by_id:
                self.created_by = user
            self.updated_by = user
        return super().save(*args, **kwargs)


class Building(TimeStampedModel):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_buildings",
        help_text="PG Admin who owns this property",
        limit_choices_to={"role": "pg_admin"},
    )
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="managed_buildings",
        null=True,
        blank=True,
        help_text="Staff or admin managing this property",
        limit_choices_to={"role": "pg_staff"},
    )

    name = models.CharField(max_length=150)
    code = models.CharField(max_length=30, blank=True, help_text="Optional unique code used internally")
    property_type = models.CharField(max_length=20, choices=PROPERTY_TYPE_CHOICES, default="boys")

    # Address fields (kept simple; can be normalized later)
    address_line = models.CharField(max_length=400)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=50)
    pincode = models.CharField(
        max_length=6,
        validators=[RegexValidator(regex=r"^\d{6}$", message="PIN code must be exactly 6 digits")],
        help_text="6-digit PIN code",
    )

    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, help_text="Optional notes about this building")

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="uniq_building_owner_name"),
        ]
        indexes = [
            models.Index(fields=["owner", "name"]),
            models.Index(fields=["city", "state"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city})"


class Floor(TimeStampedModel):
    building = models.ForeignKey(Building, on_delete=models.CASCADE, related_name="floors")
    number = models.PositiveSmallIntegerField(choices=FLOOR_CHOICES, help_text="Select floor (0 = Ground)")
    notes = models.TextField(blank=True, help_text="Optional notes about this floor")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["building", "number"]
        constraints = [
            models.UniqueConstraint(fields=["building", "number"], name="uniq_floor_building_number"),
        ]

    def __str__(self) -> str:
        return f"{self.building.name} - {self.get_number_display()}"


class Room(TimeStampedModel):
    ROOM_TYPE_CHOICES = (
        ("single_sharing", "Single Sharing"),
        ("2_sharing", "2 Sharing"),
        ("3_sharing", "3 Sharing"),
        ("4_sharing", "4 Sharing"),
        ("5_sharing", "5 Sharing"),
        ("6_sharing", "6 Sharing"),
        ("7_sharing", "7 Sharing"),
        ("8_sharing", "8 Sharing"),
        ("9_sharing", "9 Sharing"),
        ("10_sharing", "10 Sharing"),
        ("11_sharing", "11 Sharing"),
        ("12_sharing", "12 Sharing"),
        ("13_sharing", "13 Sharing"),
        ("14_sharing", "14 Sharing"),
        ("15_sharing", "15 Sharing"),
    )

    floor = models.ForeignKey(Floor, on_delete=models.CASCADE, related_name="rooms")
    number = models.CharField(max_length=20, help_text="Room number or identifier")
    room_type = models.CharField(max_length=20, choices=ROOM_TYPE_CHOICES, default="single_sharing")

    monthly_rent = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    security_deposit = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    notes = models.TextField(blank=True, help_text="Optional notes about this room")

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["floor", "number"]
        constraints = [
            models.UniqueConstraint(fields=["floor", "number"], name="uniq_room_floor_number"),
        ]

    def __str__(self) -> str:
        return f"Room {self.number} - {self.floor}"

    @property
    def capacity(self) -> int:
        """Return capacity derived from room_type (e.g., 2_sharing -> 2)."""
        if self.room_type == "single_sharing":
            return 1
        try:
            return int(self.room_type.split("_", 1)[0])
        except (ValueError, AttributeError, IndexError):
            return 1


class Bed(TimeStampedModel):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="beds")
    number = models.CharField(max_length=20, help_text="Bed number or identifier within the room")
    BED_STATUS_CHOICES = (
        ("available", "Available"),
        ("reserved", "Reserved"),
        ("occupied", "Occupied"),
        ("maintenance", "Maintenance"),
    )
    status = models.CharField(max_length=12, choices=BED_STATUS_CHOICES, default="available")
    monthly_rent = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["room", "number"]
        constraints = [
            models.UniqueConstraint(fields=["room", "number"], name="uniq_bed_room_number"),
        ]
        indexes = [
            models.Index(fields=["room", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.room} - Bed {self.number}"

    @property
    def is_available(self) -> bool:
        return self.status == "available"

    @property
    def is_reserved(self) -> bool:
        return self.status == "reserved"

    @property
    def is_occupied(self) -> bool:
        return self.status == "occupied"

    @property
    def is_under_maintenance(self) -> bool:
        return self.status == "maintenance"

    def clean(self):
        super().clean()
        if not self.room_id:
            return
        # Do not allow creating/moving a bed into a room beyond its capacity
        # Count existing beds in the target room excluding self
        existing_beds = self.room.beds.exclude(pk=self.pk).count()
        if existing_beds >= self.room.capacity:
            raise ValidationError({
                "room": f"Cannot add more beds. Room capacity is {self.room.capacity} and it already has {existing_beds} bed(s)."
            })
        # If under maintenance, require a note
        if self.status == "maintenance" and not (self.notes and self.notes.strip()):
            raise ValidationError({"notes": "Please provide a reason when setting status to maintenance."})
        # Count existing reserved/occupied beds in the same room (excluding self)
        used_qs = self.room.beds.filter(status__in=["reserved", "occupied"]).exclude(pk=self.pk)
        used_count = used_qs.count()
        # If this bed will be reserved/occupied, include it in the count
        if self.status in {"reserved", "occupied"}:
            used_count += 1
        if used_count > self.room.capacity:
            raise ValidationError({
                "status": f"Cannot set status to '{self.status}'. Reserved/occupied beds ({used_count}) exceed room capacity ({self.room.capacity})."
            })

    def save(self, *args, **kwargs):
        # Enforce validation on direct saves
        self.full_clean()
        return super().save(*args, **kwargs)

    # ---- Convenience accessors for tenant bed history ----
    @property
    def history_qs(self):
        """All history rows for this bed (most recent first)."""
        return self.usage_history.select_related("tenant").order_by("-started_on", "-created_at")

    @property
    def current_history(self):
        """Open history row (current tenant usage), if any."""
        return self.usage_history.select_related("tenant").filter(ended_on__isnull=True).order_by("-started_on").first()

    @property
    def current_tenant(self):
        """Convenience: current tenant on this bed, if any."""
        entry = self.current_history
        return entry.tenant if entry else None

    @property
    def last_history(self):
        """Most recently closed history row, if any."""
        return (
            self.usage_history.select_related("tenant")
            .filter(ended_on__isnull=False)
            .order_by("-ended_on", "-started_on")
            .first()
        )

    @property
    def last_tenant(self):
        """Convenience: previous tenant who vacated most recently, if any."""
        entry = self.last_history
        return entry.tenant if entry else None


# -------------------- Signals for Notifications --------------------

@receiver(post_save, sender=Building)
def _building_post_save_notify(sender, instance: Building, created: bool, **kwargs):
    try:
        # Recipients: owner (pg_admin), manager (if exists)
        recipients = [instance.owner]
        if instance.manager_id:
            recipients.append(instance.manager)
        payload = {
            "building": instance.name,
            "city": instance.city,
            "state": instance.state,
            "property_type": instance.property_type,
        }
        from notifications.services import notify
        if created:
            # building.created
            notify(
                event="building.created",
                recipient=recipients,
                pg_admin=instance.owner_id,
                building=instance.id,
                subject=instance,
                payload=payload,
            )
        else:
            # building.updated
            notify(
                event="building.updated",
                recipient=recipients,
                pg_admin=instance.owner_id,
                building=instance.id,
                subject=instance,
                payload=payload,
            )
    except Exception:
        # Never break persistence because of notifications
        pass


@receiver(post_save, sender=Floor)
def _floor_post_save_notify(sender, instance: Floor, created: bool, **kwargs):
    try:
        b = instance.building
        recipients = [b.owner]
        if b.manager_id:
            recipients.append(b.manager)
        payload = {
            "building": b.name,
            "floor": instance.get_number_display(),
        }
        from notifications.services import notify
        if created:
            notify(
                event="floor.created",
                recipient=recipients,
                pg_admin=b.owner_id,
                building=b.id,
                subject=instance,
                payload=payload,
            )
        else:
            notify(
                event="floor.updated",
                recipient=recipients,
                pg_admin=b.owner_id,
                building=b.id,
                subject=instance,
                payload=payload,
            )
    except Exception:
        pass


@receiver(post_save, sender=Room)
def _room_post_save_notify(sender, instance: Room, created: bool, **kwargs):
    try:
        b = instance.floor.building
        recipients = [b.owner]
        if b.manager_id:
            recipients.append(b.manager)
        payload = {
            "building": b.name,
            "floor": instance.floor.get_number_display(),
            "room": instance.number,
        }
        from notifications.services import notify
        if created:
            notify(
                event="room.created",
                recipient=recipients,
                pg_admin=b.owner_id,
                building=b.id,
                subject=instance,
                payload=payload,
            )
        else:
            notify(
                event="room.updated",
                recipient=recipients,
                pg_admin=b.owner_id,
                building=b.id,
                subject=instance,
                payload=payload,
            )
    except Exception:
        pass


@receiver(pre_save, sender=Bed)
def _bed_pre_save_track_status(sender, instance: Bed, **kwargs):
    try:
        if not instance.pk:
            instance._old_status = None
            return
        old = Bed.objects.only("status").get(pk=instance.pk)
        instance._old_status = old.status
    except Exception:
        instance._old_status = None


@receiver(post_save, sender=Bed)
def _bed_post_save_notify(sender, instance: Bed, created: bool, **kwargs):
    try:
        b = instance.room.floor.building
        recipients = [b.owner]
        if b.manager_id:
            recipients.append(b.manager)
        payload = {
            "building": b.name,
            "floor": instance.room.floor.get_number_display(),
            "room": instance.room.number,
            "bed": instance.number,
            "status": instance.status,
        }
        from notifications.services import notify
        if created:
            notify(
                event="bed.created",
                recipient=recipients,
                pg_admin=b.owner_id,
                building=b.id,
                subject=instance,
                payload=payload,
            )
            return
        old_status = getattr(instance, "_old_status", None)
        if old_status and old_status != instance.status:
            payload["old_status"] = old_status
            notify(
                event="bed.status_changed",
                recipient=recipients,
                pg_admin=b.owner_id,
                building=b.id,
                subject=instance,
                payload=payload,
            )
        else:
            # generic bed.updated for other field changes
            notify(
                event="bed.updated",
                recipient=recipients,
                pg_admin=b.owner_id,
                building=b.id,
                subject=instance,
                payload=payload,
            )
    except Exception:
        pass


@receiver(post_delete, sender=Building)
def _building_post_delete_notify(sender, instance: Building, **kwargs):
    try:
        recipients = []
        if instance.owner_id:
            recipients.append(instance.owner)
        if instance.manager_id:
            recipients.append(instance.manager)
        if not recipients:
            return
        payload = {
            "building": instance.name,
            "city": instance.city,
            "state": instance.state,
        }
        from notifications.services import notify
        notify(
            event="building.deleted",
            recipient=recipients,
            pg_admin=getattr(instance.owner, 'id', None),
            building=getattr(instance, 'id', None),
            subject=None,
            payload=payload,
        )
    except Exception:
        pass


@receiver(post_delete, sender=Floor)
def _floor_post_delete_notify(sender, instance: Floor, **kwargs):
    try:
        b = instance.building
        recipients = []
        if b and b.owner_id:
            recipients.append(b.owner)
        if b and b.manager_id:
            recipients.append(b.manager)
        if not recipients:
            return
        payload = {
            "building": b.name if b else None,
            "floor": instance.get_number_display(),
        }
        from notifications.services import notify
        notify(
            event="floor.deleted",
            recipient=recipients,
            pg_admin=b.owner_id if b else None,
            building=b.id if b else None,
            subject=None,
            payload=payload,
        )
    except Exception:
        pass


@receiver(post_delete, sender=Room)
def _room_post_delete_notify(sender, instance: Room, **kwargs):
    try:
        b = instance.floor.building if instance.floor_id else None
        recipients = []
        if b and b.owner_id:
            recipients.append(b.owner)
        if b and b.manager_id:
            recipients.append(b.manager)
        if not recipients:
            return
        payload = {
            "building": b.name if b else None,
            "floor": instance.floor.get_number_display() if instance.floor_id else None,
            "room": instance.number,
        }
        from notifications.services import notify
        notify(
            event="room.deleted",
            recipient=recipients,
            pg_admin=b.owner_id if b else None,
            building=b.id if b else None,
            subject=None,
            payload=payload,
        )
    except Exception:
        pass


@receiver(post_delete, sender=Bed)
def _bed_post_delete_notify(sender, instance: Bed, **kwargs):
    try:
        # Resolve building via relations
        b = None
        try:
            if instance.room_id and instance.room and instance.room.floor_id:
                b = instance.room.floor.building
        except Exception:
            b = None
        recipients = []
        if b and b.owner_id:
            recipients.append(b.owner)
        if b and b.manager_id:
            recipients.append(b.manager)
        if not recipients:
            return
        payload = {
            "building": b.name if b else None,
            "floor": instance.room.floor.get_number_display() if (instance.room_id and instance.room and instance.room.floor_id) else None,
            "room": instance.room.number if instance.room_id else None,
            "bed": instance.number,
        }
        from notifications.services import notify
        notify(
            event="bed.deleted",
            recipient=recipients,
            pg_admin=b.owner_id if b else None,
            building=b.id if b else None,
            subject=None,
            payload=payload,
        )
    except Exception:
        pass
