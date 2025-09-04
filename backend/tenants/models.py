from django.db import models
from django.core.validators import RegexValidator, MinValueValidator, FileExtensionValidator
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from io import BytesIO
from PIL import Image, UnidentifiedImageError
import os
from django.utils import timezone
from django.apps import apps
from django.db.models import Q
from django.db import transaction
import logging
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
from properties.models import Bed, TimeStampedModel, Building

logger = logging.getLogger(__name__)

# Reusable validators
MAX_IMAGE_UPLOAD_SIZE_MB = 4  # images
MAX_PDF_UPLOAD_SIZE_MB = 2     # PDFs

def validate_file_size(file_obj):
    if not file_obj:
        return
    # Determine limit by extension
    name = getattr(file_obj, 'name', '') or ''
    ext = os.path.splitext(name)[1].lower().lstrip('.')
    if ext == 'pdf':
        limit_mb = MAX_PDF_UPLOAD_SIZE_MB
    else:
        limit_mb = MAX_IMAGE_UPLOAD_SIZE_MB
    max_bytes = limit_mb * 1024 * 1024
    size = getattr(file_obj, 'size', None)
    if size is not None and size > max_bytes:
        raise ValidationError(f"File too large. Maximum allowed size is {limit_mb} MB for .{ext or 'file'}")

# Image optimization helpers
MAX_IMAGE_DIMENSIONS = (1600, 1600)  # width, height
DEFAULT_IMAGE_QUALITY = 75  # JPEG/WebP quality

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

def _is_image_file(file_obj) -> bool:
    if not file_obj:
        return False
    name = getattr(file_obj, 'name', '') or ''
    ext = os.path.splitext(name)[1].lower().lstrip('.')
    return ext in IMAGE_EXTENSIONS

def optimize_image_file(file_obj, target_format=None):
    """Return (bytes, new_filename) if optimized; otherwise return (None, None)."""
    if not _is_image_file(file_obj):
        return None, None
    try:
        file_obj.seek(0)
    except Exception:
        pass
    try:
        with Image.open(file_obj) as img:
            img_format = (img.format or '').upper()
            # Convert to RGB to avoid issues when saving to JPEG
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            # Resize keeping aspect ratio
            img.thumbnail(MAX_IMAGE_DIMENSIONS, Image.LANCZOS)

            # Choose a space-efficient format
            fmt = target_format or ("WEBP" if img_format not in {"JPEG", "WEBP"} else img_format)
            buffer = BytesIO()
            save_kwargs = {}
            if fmt in {"JPEG", "WEBP"}:
                save_kwargs.update({"quality": DEFAULT_IMAGE_QUALITY, "optimize": True})
                if fmt == "JPEG":
                    save_kwargs.setdefault("progressive", True)
            img.save(buffer, format=fmt, **save_kwargs)
            buffer.seek(0)

            # Build new filename with appropriate extension
            orig_name = getattr(file_obj, 'name', 'upload')
            base, _ext = os.path.splitext(orig_name)
            new_ext = ".webp" if fmt == "WEBP" else ".jpg" if fmt == "JPEG" else f".{fmt.lower()}"
            new_name = f"{base}{new_ext}"
            return buffer.read(), new_name
    except (UnidentifiedImageError, OSError):
        # Not an image or unreadable; skip optimization
        return None, None


class Tenant(TimeStampedModel):
    GENDER_CHOICES = (
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
    )

    # Basic identity
    full_name = models.CharField(max_length=200)
    email = models.EmailField(blank=True, null=True)
    phone = models.CharField(
        max_length=15,
        validators=[RegexValidator(regex=r"^\d{10}$", message="Phone number must be 10 digits")],
    )
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True)
    date_of_birth = models.DateField(blank=True, null=True)

    # Address (simple for now)
    address_line = models.CharField(max_length=400, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=50, blank=True)
    pincode = models.CharField(
        max_length=6,
        blank=True,
        validators=[RegexValidator(regex=r"^\d{6}$", message="PIN code must be exactly 6 digits")],
    )

    building = models.ForeignKey(
        Building,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="tenants",
        help_text="Home building for this tenant",
    )

    # KYC-lite
    id_proof_type = models.CharField(max_length=50, blank=True, help_text="e.g., Aadhaar, PAN, Passport")
    id_proof_number = models.CharField(max_length=100, blank=True)
    id_proof_document = models.FileField(
        upload_to="tenants/id_proofs/",
        null=True,
        blank=True,
        validators=[FileExtensionValidator(allowed_extensions=["jpg", "jpeg", "png", "pdf"]), validate_file_size],
        help_text="Upload an image (max 4 MB) or PDF (max 2 MB) for ID proof",
    )
    photo = models.ImageField(upload_to="tenants/photos/", blank=True, null=True, validators=[validate_file_size])

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["full_name", "phone"]
        indexes = [
            models.Index(fields=["phone"]),
            models.Index(fields=["email"]),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.phone})"

    def save(self, *args, **kwargs):
        # Normalize and capitalize tenant name
        if isinstance(self.full_name, str):
            # Trim and collapse whitespace
            self.full_name = " ".join(self.full_name.split())
            # Title case (basic capitalization)
            if self.full_name:
                self.full_name = self.full_name.title()
        # Keep old file names (if any) for cleanup after successful save
        old_photo_name = None
        old_id_doc_name = None
        if self.pk:
            try:
                old = Tenant.objects.get(pk=self.pk)
                old_photo_name = old.photo.name if old.photo else None
                old_id_doc_name = old.id_proof_document.name if old.id_proof_document else None
            except Tenant.DoesNotExist:
                pass

        # Optimize id_proof_document if it's an image
        if getattr(self, 'id_proof_document'):
            content, new_name = optimize_image_file(self.id_proof_document)
            if content is not None:
                self.id_proof_document.save(new_name, ContentFile(content), save=False)
        # Optimize photo
        if getattr(self, 'photo'):
            content, new_name = optimize_image_file(self.photo)
            if content is not None:
                self.photo.save(new_name, ContentFile(content), save=False)

        super().save(*args, **kwargs)

        # Cleanup old files if they were replaced
        try:
            if old_photo_name and self.photo and self.photo.name != old_photo_name:
                photo_storage = self._meta.get_field('photo').storage
                if photo_storage.exists(old_photo_name):
                    photo_storage.delete(old_photo_name)
            if old_id_doc_name and self.id_proof_document and self.id_proof_document.name != old_id_doc_name:
                doc_storage = self._meta.get_field('id_proof_document').storage
                if doc_storage.exists(old_id_doc_name):
                    doc_storage.delete(old_id_doc_name)
        except Exception:
            # Avoid breaking the request if storage cleanup fails
            pass

    def delete(self, *args, **kwargs):
        # Capture file paths before deleting the instance
        photo_name = self.photo.name if self.photo else None
        id_doc_name = self.id_proof_document.name if self.id_proof_document else None
        super().delete(*args, **kwargs)
        # Remove files from storage
        try:
            if photo_name:
                photo_storage = self._meta.get_field('photo').storage
                if photo_storage.exists(photo_name):
                    photo_storage.delete(photo_name)
            if id_doc_name:
                doc_storage = self._meta.get_field('id_proof_document').storage
                if doc_storage.exists(id_doc_name):
                    doc_storage.delete(id_doc_name)
        except Exception:
            pass


class EmergencyContact(TimeStampedModel):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="emergency_contacts")
    name = models.CharField(max_length=200)
    relationship = models.CharField(max_length=100, blank=True)
    phone = models.CharField(
        max_length=15,
        validators=[RegexValidator(regex=r"^\d{10}$", message="Phone number must be 10 digits")],
    )

    class Meta:
        ordering = ["tenant", "name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "phone"], name="uniq_tenant_emergency_phone"),
        ]

    def __str__(self) -> str:
        return f"{self.name} - {self.relationship} ({self.phone})"


class TenantBedHistory(TimeStampedModel):
    """Historical usage of a bed by a tenant.

    Auto-managed via `Stay.save()` and `Stay.delete()`:
    - On check-in/activation, opens a history row.
    - On check-out/completion or bed change, closes the previous row.
    """
    tenant = models.ForeignKey('tenants.Tenant', on_delete=models.PROTECT, related_name='bed_history')
    bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name='usage_history')
    started_on = models.DateField()
    ended_on = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'tenant_bed_history'
        ordering = ["-started_on", "-created_at"]
        indexes = [
            models.Index(fields=["tenant", "bed"], name="idx_bedhist_tenant_bed"),
            models.Index(fields=["bed", "started_on"], name="idx_bedhist_bed_start"),
        ]
        constraints = [
            # Only one open history per tenant-bed at a time
            models.UniqueConstraint(
                fields=["tenant", "bed"],
                condition=models.Q(ended_on__isnull=True),
                name="uniq_open_bedhistory_per_tenant_bed",
            )
        ]

    def __str__(self) -> str:
        return f"{self.tenant} • {self.bed} • {self.started_on} → {self.ended_on or 'present'}"


class BedHistory(TenantBedHistory):
    """Proxy model to represent bed-centric history records.

    This does not create a new table. It simply exposes the same rows as
    `TenantBedHistory` but can be used in admin, queries, or views when a
    bed-focused naming is preferred.
    """

    class Meta:
        proxy = True
        verbose_name = "Bed history"
        verbose_name_plural = "Bed histories"
        ordering = ["-started_on", "-created_at"]


class Stay(TimeStampedModel):
    """
    Represents a reservation/occupancy for a tenant on a specific bed.
    Use status to track lifecycle. Keeps a snapshot of rent/deposit at booking time.
    """

    STATUS_CHOICES = (
        ("reserved", "Reserved"),
        ("active", "Active / Checked-in"),
        ("completed", "Completed / Checked-out"),
        ("canceled", "Canceled"),
    )

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="stays")
    bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name="stays")

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="reserved")

    check_in = models.DateField()
    expected_check_out = models.DateField(blank=True, null=True)
    actual_check_out = models.DateField(blank=True, null=True)

    monthly_rent = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    security_deposit = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    maintenance_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)], help_text="One-time or recurring maintenance charge applicable for this stay")

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["bed", "status"]),
            models.Index(fields=["check_in"]),
        ]
        constraints = [
            # Only one active or reserved stay per tenant
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(status__in=["reserved", "active"]),
                name="uniq_active_reserved_stay_per_tenant",
            ),
            # Only one active or reserved stay per bed
            models.UniqueConstraint(
                fields=["bed"],
                condition=models.Q(status__in=["reserved", "active"]),
                name="uniq_active_reserved_stay_per_bed",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tenant.full_name} - Bed {self.bed.number} ({self.status})"

    def clean(self):
        super().clean()
        # Date validations
        if self.actual_check_out and self.actual_check_out < self.check_in:
            raise ValidationError({"actual_check_out": "Actual checkout cannot be before check-in."})
        if self.expected_check_out and self.expected_check_out < self.check_in:
            raise ValidationError({"expected_check_out": "Expected checkout cannot be before check-in."})

        # Prevent overlapping reserved/active stays on the same bed (enforced again by unique constraints)
        if self.status in {"reserved", "active"}:
            bed_conflict = (
                Stay.objects.filter(bed=self.bed, status__in=["reserved", "active"]).exclude(pk=self.pk).exists()
                if self.bed_id
                else False
            )
            if bed_conflict:
                raise ValidationError({"bed": "This bed already has an active or reserved stay."})

            tenant_conflict = (
                Stay.objects.filter(tenant=self.tenant, status__in=["reserved", "active"]).exclude(pk=self.pk).exists()
                if self.tenant_id
                else False
            )
            if tenant_conflict:
                raise ValidationError({"tenant": "This tenant already has an active or reserved stay."})

        # Bed status sanity: discourage assigning to maintenance bed
        if self.bed_id and self.bed.status == "maintenance":
            raise ValidationError({"bed": "Cannot assign a tenant to a bed under maintenance."})

    def save(self, *args, **kwargs):
        # default monthly_rent from bed if not provided
        if self.monthly_rent is None and self.bed_id:
            self.monthly_rent = self.bed.monthly_rent or 0
        # Capture previous state for history handling
        old_bed_id = None
        old_status = None
        if self.pk:
            try:
                old = Stay.objects.get(pk=self.pk)
                old_bed_id = old.bed_id
                old_status = old.status
            except Stay.DoesNotExist:
                pass
        # run validations and save
        self.full_clean()
        super().save(*args, **kwargs)

        # --- Auto-manage TenantBedHistory ---
        today = timezone.localdate()

        def _close_open_history(tenant_id, bed_id, end_date):
            if not tenant_id or not bed_id:
                logger.debug("[Stay.save] Skip close history: missing tenant_id(%s) or bed_id(%s)", tenant_id, bed_id)
                return
            try:
                entry = TenantBedHistory.objects.filter(tenant_id=tenant_id, bed_id=bed_id, ended_on__isnull=True).latest("started_on")
                if not entry.ended_on:
                    entry.ended_on = end_date or today
                    entry.save(update_fields=["ended_on", "updated_at"])
                    logger.debug("[Stay.save] Closed history tenant=%s bed=%s start=%s end=%s", tenant_id, bed_id, entry.started_on, entry.ended_on)
            except TenantBedHistory.DoesNotExist:
                logger.debug("[Stay.save] No open history to close for tenant=%s bed=%s", tenant_id, bed_id)

        def _open_history(tenant_id, bed_id, start_date):
            if not tenant_id or not bed_id:
                logger.debug("[Stay.save] Skip open history: missing tenant_id(%s) or bed_id(%s)", tenant_id, bed_id)
                return
            # Avoid duplicate open rows
            exists = TenantBedHistory.objects.filter(tenant_id=tenant_id, bed_id=bed_id, ended_on__isnull=True).exists()
            if not exists:
                TenantBedHistory.objects.create(
                    tenant_id=tenant_id,
                    bed_id=bed_id,
                    started_on=start_date or today,
                )
                logger.debug("[Stay.save] Opened history tenant=%s bed=%s start=%s", tenant_id, bed_id, start_date or today)
            else:
                logger.debug("[Stay.save] Open history already exists for tenant=%s bed=%s", tenant_id, bed_id)

        # If bed changed, close old and open new based on current status/check-in
        if old_bed_id and old_bed_id != self.bed_id:
            logger.debug("[Stay.save] Bed changed for stay=%s tenant=%s: %s -> %s; status=%s", self.pk, self.tenant_id, old_bed_id, self.bed_id, self.status)
            # Close old usage
            end_date = self.actual_check_out or today
            _close_open_history(self.tenant_id, old_bed_id, end_date)
            # Open new usage only if reserved/active
            if self.status in {"reserved", "active"}:
                start_date = self.check_in or today
                _open_history(self.tenant_id, self.bed_id, start_date)

        # Handle status transitions and creation for same bed
        if (old_status != self.status) or self._state.adding:
            logger.debug("[Stay.save] Status change/add for stay=%s tenant=%s bed=%s: %s -> %s", self.pk, self.tenant_id, self.bed_id, old_status, self.status)
            if self.status in {"reserved", "active"}:
                # Ensure an open history exists starting from check_in (or today fallback)
                _open_history(self.tenant_id, self.bed_id, self.check_in or today)
            elif self.status == "completed":
                # Close current open history on checkout
                _close_open_history(self.tenant_id, self.bed_id, self.actual_check_out or today)

        # Recompute Bed.status for both old and new beds
        try:
            with transaction.atomic():
                if old_bed_id and old_bed_id != self.bed_id:
                    _recompute_bed_status_for_tenants(old_bed_id)
                if self.bed_id:
                    _recompute_bed_status_for_tenants(self.bed_id)
        except Exception as e:
            logger.exception("[Stay.save] Failed recomputing bed status for bed(s) old=%s new=%s: %s", old_bed_id, self.bed_id, e)

        return self

    def delete(self, *args, **kwargs):
        # On deletion, close any open history for this tenant/bed
        _ = super()
        try:
            _close = TenantBedHistory.objects.filter(tenant_id=self.tenant_id, bed_id=self.bed_id, ended_on__isnull=True)
            count = _close.count()
            for entry in _close:
                entry.ended_on = timezone.localdate()
                entry.save(update_fields=["ended_on", "updated_at"])
            logger.debug("[Stay.delete] Closed %s open history rows for tenant=%s bed=%s", count, self.tenant_id, self.bed_id)
        except Exception as e:
            logger.exception("[Stay.delete] Error closing open history for tenant=%s bed=%s: %s", self.tenant_id, self.bed_id, e)
        # Recompute bed status after deletion
        try:
            if self.bed_id:
                _recompute_bed_status_for_tenants(self.bed_id)
        except Exception as e:
            logger.exception("[Stay.delete] Error recomputing bed status for bed=%s: %s", self.bed_id, e)
        return super().delete(*args, **kwargs)


# Local helper to recompute bed status without importing Booking directly (avoid circular imports)
def _recompute_bed_status_for_tenants(bed_id: int):
    if not bed_id:
        return
    try:
        bed = Bed.objects.select_for_update().get(pk=bed_id)
    except Bed.DoesNotExist:
        return

    # Respect maintenance; do not override
    if bed.status == "maintenance":
        return

    # Decide strictly from bookings overlapping today
    Booking = apps.get_model("bookings", "Booking")
    today = timezone.localdate()
    overlap_q = Q(start_date__lte=today) & (Q(end_date__isnull=True) | Q(end_date__gte=today))
    has_confirmed_today = Booking.objects.filter(bed_id=bed_id, status="confirmed").filter(overlap_q).exists()
    if has_confirmed_today:
        new_status = "occupied"
    else:
        has_reserved_today = Booking.objects.filter(bed_id=bed_id, status="reserved").filter(overlap_q).exists()
        new_status = "reserved" if has_reserved_today else "available"

    if bed.status != new_status:
        bed.status = new_status
        bed.save(update_fields=["status", "updated_at"]) 


# -------------------- Stay Signals for Tenant Status Management --------------------

@receiver(post_save, sender=Stay)
def _stay_post_save_update_tenant_status(sender, instance: Stay, created: bool, **kwargs):
    """Update tenant is_active status based on stay status changes."""
    try:
        tenant = instance.tenant
        if not tenant:
            return
            
        # Check if tenant has any active or reserved stays
        has_active_stays = tenant.stays.filter(status__in=['active', 'reserved']).exists()
        
        # Update tenant status based on stay status
        new_is_active = has_active_stays
        
        # Only update if status actually changed to avoid unnecessary saves
        if tenant.is_active != new_is_active:
            tenant.is_active = new_is_active
            tenant.save(update_fields=['is_active', 'updated_at'])
            logger.debug(
                "Updated tenant %s is_active to %s based on stay status change",
                tenant.id, new_is_active
            )
    except Exception as e:
        logger.exception("Error updating tenant status after stay change: %s", e)

@receiver(post_delete, sender=Stay)
def _stay_post_delete_update_tenant_status(sender, instance: Stay, **kwargs):
    """Update tenant is_active status when a stay is deleted."""
    try:
        tenant = instance.tenant
        if not tenant:
            return
            
        # Check if tenant has any remaining active or reserved stays
        has_active_stays = tenant.stays.filter(status__in=['active', 'reserved']).exists()
        
        # Update tenant status
        new_is_active = has_active_stays
        
        # Only update if status actually changed
        if tenant.is_active != new_is_active:
            tenant.is_active = new_is_active
            tenant.save(update_fields=['is_active', 'updated_at'])
            logger.debug(
                "Updated tenant %s is_active to %s after stay deletion",
                tenant.id, new_is_active
            )
    except Exception as e:
        logger.exception("Error updating tenant status after stay deletion: %s", e)


# -------------------- Tenant Signals for Notifications --------------------

@receiver(pre_save, sender=Tenant)
def _tenant_pre_save_track(sender, instance: Tenant, **kwargs):
    try:
        if not instance.pk:
            instance._old_building_id = None
            instance._old_is_active = None
            return
        old = Tenant.objects.only("building_id", "is_active").get(pk=instance.pk)
        instance._old_building_id = old.building_id
        instance._old_is_active = old.is_active
    except Exception:
        instance._old_building_id = None
        instance._old_is_active = None

@receiver(post_save, sender=Tenant)
def _tenant_post_save_notify(sender, instance: Tenant, created: bool, **kwargs):
    try:
        b = instance.building  # can be None
        # Determine pg_admin owner and manager from building, if present
        pg_admin_id = b.owner_id if b else None
        recipients = []
        if b:
            if b.owner_id:
                recipients.append(b.owner)
            if b.manager_id:
                recipients.append(b.manager)
        # Fallback: if no building yet, notify the creating admin? Skip to avoid ambiguity
        if not recipients:
            return

        payload = {
            "tenant": instance.full_name,
            "phone": instance.phone,
            "email": instance.email or "",
            "building": b.name if b else None,
        }
        from notifications.services import notify
        if created:
            notify(
                event="tenant.created",
                recipient=recipients,
                pg_admin=pg_admin_id,
                building=b.id if b else None,
                subject=instance,
                payload=payload,
            )
            return

        # building move
        old_bid = getattr(instance, "_old_building_id", None)
        if old_bid is not None and old_bid != instance.building_id:
            # try to get old/new names
            old_name = None
            try:
                if old_bid:
                    old_b = Building.objects.only("name").get(pk=old_bid)
                    old_name = old_b.name
            except Building.DoesNotExist:
                pass
            payload.update({
                "old_building_id": old_bid,
                "old_building": old_name,
                "new_building_id": instance.building_id,
                "new_building": b.name if b else None,
            })
            notify(
                event="tenant.moved",
                recipient=recipients,
                pg_admin=pg_admin_id,
                building=b.id if b else None,
                subject=instance,
                payload=payload,
            )
            return

        # status change
        old_active = getattr(instance, "_old_is_active", None)
        if old_active is not None and old_active != instance.is_active:
            payload.update({
                "old_is_active": old_active,
                "new_is_active": instance.is_active,
                "status": "active" if instance.is_active else "inactive",
            })
            notify(
                event="tenant.status_changed",
                recipient=recipients,
                pg_admin=pg_admin_id,
                building=b.id if b else None,
                subject=instance,
                payload=payload,
            )
    except Exception:
        # Never block persistence due to notifications
        pass
