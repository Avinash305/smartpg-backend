from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db.models import Q
from django.core.validators import MinValueValidator
from django.conf import settings
from accounts.middleware import get_current_user
from django.db import transaction
from django.db.models.signals import post_delete, pre_save, post_save
from django.dispatch import receiver

from properties.models import TimeStampedModel, Building, Floor, Room, Bed
from tenants.models import Tenant, Stay


class Booking(TimeStampedModel):
    """
    A lightweight reservation object that precedes a confirmed stay.
    It references existing Property (Building/Room/Bed) and Tenant models.
    Use status to manage lifecycle; once checked-in, you may convert to a `tenants.Stay`.
    """

    STATUS_CHOICES = (
        ("reserved", "Reserved"),
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("canceled", "Canceled"),
        ("converted", "Converted to Stay"),
        ("checked_out", "Checked Out"),
    )

    SOURCE_CHOICES = (
        ("walkin", "Walk-in"),
        ("phone", "Phone"),
        ("online", "Online"),
        ("other", "Other"),
    )

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="bookings")

    # Location breakdown (building/room are for convenience/filters; bed is the actual resource)
    building = models.ForeignKey(Building, on_delete=models.PROTECT, related_name="bookings")
    floor = models.ForeignKey(Floor, on_delete=models.PROTECT, related_name="bookings")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="bookings")
    bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name="bookings")

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending")
    source = models.CharField(max_length=12, choices=SOURCE_CHOICES, default="walkin", blank=True)

    # Reservation window
    start_date = models.DateField()
    end_date = models.DateField(blank=True, null=True, help_text="Optional end date for reservation window")

    # Pricing snapshot at the time of booking
    monthly_rent = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)], help_text="Defaults from room's monthly rent; editable")
    security_deposit = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)], help_text="Defaults from room's security deposit; editable")
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)], help_text="Discount applied at booking (INR)")
    maintenance_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)], help_text="Any maintenance charge applicable for this booking (INR)")
    notes = models.TextField(blank=True)

    # Explicit booking timestamp (separate from created_at)
    booked_at = models.DateTimeField(auto_now_add=True)
    # Explicit user who made the booking (separate from created_by)
    booked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bookings_booked",
        help_text="User who made this booking",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["bed", "status"]),
            models.Index(fields=["start_date"]),
            models.Index(fields=["floor"]),
            models.Index(fields=["booked_at"]),
            models.Index(fields=["booked_by"]),
            models.Index(fields=["building"]),
            models.Index(fields=["room"]),
        ]

    def __str__(self) -> str:
        return f"Booking: {self.tenant.full_name} -> Bed {self.bed.number} ({self.status})"

    def clean(self):
        super().clean()

        # Basic date validation
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before start date."})

        # Transition rule: allow setting checked_out only from confirmed
        if self.status == "checked_out":
            if not self.pk:
                raise ValidationError({"status": "Cannot create a booking directly as Checked Out."})
            try:
                current = Booking.objects.only("status").get(pk=self.pk)
                if current.status != "confirmed":
                    raise ValidationError({"status": "Checked Out can be set only from Confirmed status."})
            except Booking.DoesNotExist:
                raise ValidationError({"status": "Invalid booking for Checked Out transition."})

        # Consistency: ensure building/floor/room align with bed
        if self.bed_id:
            # Room must match bed.room
            if not self.room_id or self.bed.room_id != self.room_id:
                raise ValidationError({"room": "Selected room does not match the bed's room."})
            # Floor must match room.floor
            bed_floor_id = self.bed.room.floor_id if self.bed and self.bed.room else None
            if not self.floor_id or bed_floor_id != self.floor_id:
                raise ValidationError({"floor": "Selected floor does not match the bed's floor."})
            # Building must match floor.building
            bed_building_id = self.bed.room.floor.building_id if self.bed and self.bed.room and self.bed.room.floor else None
            if not self.building_id or bed_building_id != self.building_id:
                raise ValidationError({"building": "Selected building does not match the bed's building."})

            # Disallow booking a bed under maintenance
            if self.bed.status == "maintenance":
                raise ValidationError({"bed": "Cannot book a bed under maintenance."})

        # If bed is not set yet, still validate the hierarchy coherence if provided
        if self.room_id and self.floor_id:
            if self.room.floor_id != self.floor_id:
                raise ValidationError({"room": "Selected room does not belong to the selected floor."})
        if self.floor_id and self.building_id:
            if self.floor.building_id != self.building_id:
                raise ValidationError({"floor": "Selected floor does not belong to the selected building."})

        # Prevent overlapping bookings for the same bed when this booking is active/reserved (pending/confirmed/reserved)
        if self.status in {"pending", "confirmed", "reserved"} and self.bed_id and self.start_date:
            overlap_q = Q()
            if self.end_date:
                # (start <= existing_end) AND (end >= existing_start)
                overlap_q = Q(start_date__lte=self.end_date) & (Q(end_date__isnull=True) | Q(end_date__gte=self.start_date))
            else:
                # Open-ended booking overlaps with any that starts on/after our start or with open end
                overlap_q = Q(end_date__isnull=True) | Q(end_date__gte=self.start_date)

            conflict = (
                Booking.objects.filter(
                    bed=self.bed,
                    status__in=["pending", "confirmed", "reserved"],
                )
                .filter(overlap_q)
                .exclude(pk=self.pk)
                .exists()
            )
            if conflict:
                raise ValidationError({"bed": "This bed already has an overlapping pending/confirmed/reserved booking."})

        # Cross-check with active stays to avoid reserving an already occupied bed
        if self.bed_id and self.start_date:
            stay_overlap_q = Q()
            # For Stay, treat expected_check_out (or actual) as end; if both null, consider active indefinitely
            # We check if the booking start falls within an active/reserved stay window, or ranges overlap
            if self.end_date:
                stay_overlap_q = (
                    Q(check_in__lte=self.end_date)
                    & (
                        Q(actual_check_out__isnull=True, expected_check_out__isnull=True)
                        | Q(actual_check_out__gte=self.start_date)
                        | Q(expected_check_out__gte=self.start_date)
                    )
                )
            else:
                stay_overlap_q = (
                    Q(actual_check_out__isnull=True, expected_check_out__isnull=True)
                    | Q(actual_check_out__gte=self.start_date)
                    | Q(expected_check_out__gte=self.start_date)
                )

            stay_conflict = (
                Stay.objects.filter(
                    bed=self.bed,
                    status__in=["reserved", "active"],
                )
                .filter(stay_overlap_q)
                .exists()
            )
            if stay_conflict:
                raise ValidationError({"bed": "This bed has an active/reserved stay overlapping with the booking window."})

    def save(self, *args, **kwargs):
        # Capture old bed for later recompute
        old_bed_id = None
        if self.pk:
            try:
                old = Booking.objects.only("bed_id").get(pk=self.pk)
                old_bed_id = old.bed_id
            except Booking.DoesNotExist:
                old_bed_id = None

        # Default pricing snapshot from ROOM if not provided (but keep user edits)
        if self.room_id:
            if (self.monthly_rent or 0) <= 0:
                self.monthly_rent = self.room.monthly_rent or 0
            if (self.security_deposit or 0) < 0 or (self.security_deposit or 0) == 0:
                self.security_deposit = getattr(self.room, "security_deposit", 0) or 0

        # Pre-save: try to set booked_by from current user if not provided
        if not self.booked_by_id:
            user = get_current_user()
            if user and getattr(user, "is_authenticated", False):
                self.booked_by = user

        # Backward compatibility: map deprecated 'no_show' to 'pending'
        if self.status == "no_show":
            self.status = "pending"

        # If user is checking out and end_date not set, set to today (local date) before validation
        if self.status == "checked_out" and not self.end_date:
            today = timezone.localdate()
            self.end_date = today

        # Do not auto-derive status; honor user selection. Validation still prevents conflicts.

        # Run validations then save (this will also set created_by/updated_by in TimeStampedModel)
        self.full_clean()
        super().save(*args, **kwargs)

        # Post-save fallback: if booked_by still not set, mirror created_by
        if not self.booked_by_id and getattr(self, "created_by_id", None):
            self.booked_by_id = self.created_by_id
            super().save(update_fields=["booked_by"])  # minimal update

        # Recompute bed status for both old and new beds inside a transaction to keep consistency
        try:
            with transaction.atomic():
                if old_bed_id and old_bed_id != self.bed_id:
                    _recompute_bed_status(old_bed_id)
                if self.bed_id:
                    _recompute_bed_status(self.bed_id)
        except Exception:
            # Do not block booking save on bed status recompute issues
            pass
        return self


class Payment(TimeStampedModel):
    """Simple payment record tied to a booking."""

    METHOD_CHOICES = (
        ("cash", "Cash"),
        ("upi", "UPI"),
        ("card", "Card"),
        ("bank_transfer", "Bank Transfer"),
        ("other", "Other"),
    )

    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("success", "Success"),
        ("failed", "Failed"),
        ("refunded", "Refunded"),
    )

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="payments")
    # Optional linkage to a Stay for rent reconciliation
    stay = models.ForeignKey(Stay, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default="cash")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="success")

    paid_on = models.DateTimeField(default=timezone.now)
    reference = models.CharField(max_length=100, blank=True, help_text="Txn/Ref ID if applicable")
    # Period marker for rent payments (YYYY-MM)
    billing_period = models.CharField(max_length=7, blank=True, help_text="YYYY-MM period for rent")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-paid_on"]
        indexes = [
            models.Index(fields=["booking", "status"]),
            models.Index(fields=["paid_on"]),
            models.Index(fields=["stay", "billing_period"], name="idx_payment_stay_period"),
        ]

    def __str__(self) -> str:
        base = f"Payment ₹{self.amount} for booking {self.booking_id}"
        if self.stay_id:
            base += f" • stay {self.stay_id}"
        if self.billing_period:
            base += f" • {self.billing_period}"
        return f"{base} ({self.status})"


# NEW: Movement history of bookings
class BookingMovement(TimeStampedModel):
    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="movements")
    moved_at = models.DateTimeField(default=timezone.now)

    # Tenant change (optional)
    old_tenant = models.ForeignKey(Tenant, on_delete=models.SET_NULL, null=True, blank=True, related_name="old_tenant_moves")
    new_tenant = models.ForeignKey(Tenant, on_delete=models.SET_NULL, null=True, blank=True, related_name="new_tenant_moves")

    # From location (nullable)
    from_building = models.ForeignKey(Building, on_delete=models.SET_NULL, null=True, blank=True, related_name="from_building_moves")
    from_floor = models.ForeignKey(Floor, on_delete=models.SET_NULL, null=True, blank=True, related_name="from_floor_moves")
    from_room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, related_name="from_room_moves")
    from_bed = models.ForeignKey(Bed, on_delete=models.SET_NULL, null=True, blank=True, related_name="from_bed_moves")

    # To location (nullable)
    to_building = models.ForeignKey(Building, on_delete=models.SET_NULL, null=True, blank=True, related_name="to_building_moves")
    to_floor = models.ForeignKey(Floor, on_delete=models.SET_NULL, null=True, blank=True, related_name="to_floor_moves")
    to_room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True, related_name="to_room_moves")
    to_bed = models.ForeignKey(Bed, on_delete=models.SET_NULL, null=True, blank=True, related_name="to_bed_moves")

    notes = models.TextField(blank=True)
    moved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking_moves_made",
        help_text="User who performed this move",
    )

    class Meta:
        ordering = ["-moved_at", "-created_at"]
        indexes = [
            models.Index(fields=["booking", "moved_at"]),
            models.Index(fields=["from_bed", "to_bed"]),
            models.Index(fields=["moved_by"]),
        ]

    def __str__(self) -> str:
        return f"Move for booking {self.booking_id} at {self.moved_at}"


# ---- Helpers to keep Bed.status in sync with Bookings and Stays ----
def _recompute_bed_status(bed_id: int):
    if not bed_id:
        return
    try:
        bed = Bed.objects.select_for_update().get(pk=bed_id)
    except Bed.DoesNotExist:
        return

    # Respect manual maintenance state; never auto-override it
    if bed.status == "maintenance":
        return

    # Decide strictly from bookings overlapping today
    today = timezone.localdate()
    overlap_q = models.Q(start_date__lte=today) & (models.Q(end_date__isnull=True) | models.Q(end_date__gte=today))
    has_confirmed_today = Booking.objects.filter(bed_id=bed_id, status="confirmed").filter(overlap_q).exists()
    if has_confirmed_today:
        new_status = "occupied"
    else:
        has_reserved_today = Booking.objects.filter(bed_id=bed_id, status="reserved").filter(overlap_q).exists()
        new_status = "reserved" if has_reserved_today else "available"

    if bed.status != new_status:
        bed.status = new_status
        bed.save(update_fields=["status", "updated_at"])


@receiver(post_delete, sender=Booking)
def _booking_post_delete(sender, instance: Booking, **kwargs):
    # When a booking is deleted, re-evaluate the bed status
    try:
        if instance and instance.bed_id:
            _recompute_bed_status(instance.bed_id)
    except Exception:
        pass


# ---- Notification signal hooks ----
@receiver(pre_save, sender=Booking)
def _booking_pre_save(sender, instance: Booking, **kwargs):
    """Track previous status to detect transitions in post_save."""
    if not instance.pk:
        instance._old_status = None
        return
    try:
        old = Booking.objects.only("status").get(pk=instance.pk)
        instance._old_status = old.status
    except Booking.DoesNotExist:
        instance._old_status = None


@receiver(post_save, sender=Booking)
def _booking_post_save_notify(sender, instance: Booking, created: bool, **kwargs):
    try:
        bldg = instance.building
        pg_admin = bldg.owner if bldg else None
        recipients = []
        if pg_admin:
            recipients.append(pg_admin)
        if getattr(bldg, "manager", None):
            recipients.append(bldg.manager)

        # local import to avoid circulars
        from notifications.services import notify

        payload = {
            "booking_id": instance.pk,
            "tenant": getattr(instance.tenant, "full_name", None),
            "bed": getattr(instance.bed, "number", None),
            "room": getattr(instance.room, "number", None),
            "building": getattr(bldg, "name", None),
            "status": instance.status,
            "start_date": str(instance.start_date) if instance.start_date else None,
            "end_date": str(instance.end_date) if instance.end_date else None,
        }

        if created:
            notify(
                event="booking.created",
                recipient=recipients,
                actor=instance.booked_by or instance.created_by,
                title="New booking created",
                message=f"Booking for {instance.tenant.full_name} in {bldg.name if bldg else 'N/A'} created.",
                level="info",
                subject=instance,
                pg_admin=pg_admin,
                building=bldg,
                payload=payload,
                channels=["in_app"],
            )
            return

        # Status change
        old_status = getattr(instance, "_old_status", None)
        if old_status and old_status != instance.status:
            notify(
                event="booking.status_changed",
                recipient=recipients,
                actor=instance.booked_by or instance.updated_by,
                title="Booking status updated",
                message=f"Booking status changed from {old_status} to {instance.status} for {instance.tenant.full_name}.",
                level="info",
                subject=instance,
                pg_admin=pg_admin,
                building=bldg,
                payload={**payload, "old_status": old_status},
                channels=["in_app"],
            )
    except Exception:
        # Never break request flow on notification errors
        pass


@receiver(pre_save, sender=Payment)
def _payment_pre_save(sender, instance: Payment, **kwargs):
    """Track previous payment status to detect transitions."""
    if not instance.pk:
        instance._old_status = None
        return
    try:
        old = Payment.objects.only("status").get(pk=instance.pk)
        instance._old_status = old.status
    except Payment.DoesNotExist:
        instance._old_status = None


@receiver(post_save, sender=Payment)
def _payment_post_save_notify(sender, instance: Payment, created: bool, **kwargs):
    try:
        booking = instance.booking
        bldg = booking.building if booking else None
        pg_admin = bldg.owner if bldg else None
        recipients = []
        if pg_admin:
            recipients.append(pg_admin)
        if getattr(bldg, "manager", None):
            recipients.append(bldg.manager)

        # Reconcile invoice if possible
        try:
            if instance.status == "success" and instance.stay_id and instance.billing_period:
                from payment.models import Invoice  # local import to avoid circular
                inv = Invoice.objects.filter(stay_id=instance.stay_id, period=instance.billing_period).first()
                if inv and inv.status in {"open", "overdue"}:
                    new_balance = (inv.balance or inv.amount) - instance.amount
                    inv.balance = new_balance
                    if new_balance <= 0:
                        inv.status = "paid"
                        inv.paid_on = timezone.localdate()
                    inv.save(update_fields=["balance", "status", "paid_on", "updated_at"])
        except Exception:
            # never break notifications due to reconciliation errors
            pass

        # local import to avoid circulars
        from notifications.services import notify

        status = instance.status
        old_status = getattr(instance, "_old_status", None)
        status_changed = (not created) and old_status is not None and old_status != status

        event_map = {
            "success": "payment.success",
            "failed": "payment.failed",
            "refunded": "payment.refunded",
        }

        should_notify = False
        event_name = None
        if created and status in event_map:
            should_notify = True
            event_name = event_map[status]
        elif status_changed and status in event_map:
            should_notify = True
            event_name = event_map[status]

        if not should_notify:
            return

        payload = {
            "payment_id": instance.pk,
            "booking_id": booking.pk if booking else None,
            "stay_id": instance.stay_id,
            "billing_period": instance.billing_period,
            "amount": float(instance.amount),
            "method": instance.method,
            "reference": instance.reference,
            "tenant": getattr(booking.tenant, "full_name", None) if booking else None,
            "old_status": old_status,
            "status": status,
        }

        notify(
            event=event_name,
            recipient=recipients,
            actor=getattr(booking, "booked_by", None) or getattr(booking, "created_by", None),
            subject=instance,
            pg_admin=pg_admin,
            building=bldg,
            payload=payload,
            channels=["in_app"],
        )
    except Exception:
        pass

# ---- Keep Tenant.is_active in sync with Bookings and Stays ----
def _recompute_tenant_active_for_booking(tenant_id: int | None):
    """Set tenant.is_active = True if tenant has any live bookings or stays.

    Live bookings: status in {pending, confirmed, reserved}
    Live stays: status in {reserved, active}
    Otherwise: inactive.
    """
    if not tenant_id:
        return
    try:
        tenant = Tenant.objects.get(pk=tenant_id)
    except Tenant.DoesNotExist:
        return
    try:
        has_live_bookings = tenant.bookings.filter(status__in=["pending", "confirmed", "reserved"]).exists()
        has_live_stays = tenant.stays.filter(status__in=["reserved", "active"]).exists()
        new_active = has_live_bookings or has_live_stays
        if tenant.is_active != new_active:
            tenant.is_active = new_active
            tenant.save(update_fields=["is_active", "updated_at"])
    except Exception:
        # Do not break request on status recompute issues
        pass


@receiver(post_save, sender=Booking)
def _booking_post_save_update_tenant_status(sender, instance: Booking, created: bool, **kwargs):
    """After any booking change, recompute tenant active flag.

    Covers transitions to checked_out/canceled as well as creation of new live bookings.
    """
    try:
        _recompute_tenant_active_for_booking(getattr(instance, "tenant_id", None))
    except Exception:
        pass


@receiver(post_delete, sender=Booking)
def _booking_post_delete_update_tenant_status(sender, instance: Booking, **kwargs):
    """Also recompute on delete, in case the last live booking is removed."""
    try:
        _recompute_tenant_active_for_booking(getattr(instance, "tenant_id", None))
    except Exception:
        pass
