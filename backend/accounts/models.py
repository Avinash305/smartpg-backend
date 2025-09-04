from django.db import models, transaction
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import RegexValidator
from django.utils import timezone
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.conf import settings
from django.utils.text import slugify
import os
from tenants.models import validate_file_size

def profile_picture_upload_to(instance: "User", filename: str) -> str:
    """
    Build a deterministic, readable filename for profile pictures:
    - Base name: user's full_name; if empty, use email local part (before @), trimmed.
    - Slugified, lowercase.
    - Append user id and timestamp to avoid collisions.
    - Preserve original file extension.
    Example: profile_pictures/john-doe_u27_20250101T101530.jpg
    """
    base, ext = os.path.splitext(filename or '')
    ext = (ext or '').lower() or '.jpg'
    name = (getattr(instance, 'full_name', '') or '').strip()
    if not name:
        email = (getattr(instance, 'email', '') or '').strip()
        name = email.split('@')[0] if email else 'user'
    slug = slugify(name) or 'user'
    user_id = getattr(instance, 'pk', None) or getattr(instance, 'id', None) or 'u'
    ts = timezone.now().strftime('%Y%m%dT%H%M%S')
    filename_slug = f"{slug}_u{user_id}_{ts}{ext}"
    return os.path.join('profile_pictures', filename_slug)

class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The Email field must be set')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', 'pg_admin')
        return self.create_user(email, password, **extra_fields)

class User(AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = (
        ('pg_admin', 'PG Admin'),
        ('pg_staff', 'PG Staff'),
    )
    
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        validators=[
            RegexValidator(
                regex=r'^\d{10}$',
                message='Phone number must be 10 digits'
            )
        ]
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='pg_admin')
    password_reset_sent_at = models.DateTimeField(null=True, blank=True, help_text='When the last password reset email was sent')
    profile_picture = models.ImageField(
        upload_to=profile_picture_upload_to,
        null=True,
        blank=True,
        validators=[validate_file_size],
        help_text='Profile picture of the user'
    )
    pg_admin = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True, 
        blank=True,
        related_name='staff_members',
        limit_choices_to={'role': 'pg_admin'}
    )
    hierarchical_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    # Important: default non-staff for security; only explicit admins/superusers should be staff
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    # Add custom related names to avoid clashes with auth.User
    groups = models.ManyToManyField(
        'auth.Group',
        verbose_name='groups',
        blank=True,
        help_text='The groups this user belongs to.',
        related_name='custom_user_set',
        related_query_name='user',
    )
    user_permissions = models.ManyToManyField(
        'auth.Permission',
        verbose_name='user permissions',
        blank=True,
        help_text='Specific permissions for this user.',
        related_name='custom_user_set',
        related_query_name='user',
    )

    # Custom dynamic permissions stored as JSON
    # Shape example:
    # {
    #   "<building_id>": {
    #     "tenants": {"view": true, "add": false, "edit": false, "delete": false},
    #     "bookings": {"view": true, "add": true, "edit": true, "delete": false}
    #   },
    #   "global": { ... }  // optional global/defaults
    # }
    permissions = models.JSONField(default=dict, blank=True)
    # Per-user language preference (ISO code like 'en', 'hi', etc.)
    language = models.CharField(max_length=10, default='en')
    email_verified = models.BooleanField(default=False)
    email_otp = models.CharField(max_length=6, null=True, blank=True)
    email_otp_expires_at = models.DateTimeField(null=True, blank=True)
    email_otp_last_sent_at = models.DateTimeField(null=True, blank=True)
    email_otp_attempts = models.IntegerField(default=0)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        ordering = ['hierarchical_id']  # Order users by hierarchical_id

    def save(self, *args, **kwargs):
        # Only set hierarchical_id for new instances
        if not self.pk:
            self._set_hierarchical_id()
        super().save(*args, **kwargs)

    def _set_hierarchical_id(self):
        """
        Sets the hierarchical ID based on the user's role and parent admin.
        For PG Admins: Simple incrementing number (1, 2, 3, ...)
        For PG Staff: Parent's ID.staff_number (e.g., 1.1, 1.2, 2.1, ...)
        """
        if self.role == 'pg_admin':
            # Consider only non-null, purely numeric hierarchical IDs and compute the max
            existing_ids = (
                User.objects
                .filter(role='pg_admin')
                .exclude(hierarchical_id__isnull=True)
                .values_list('hierarchical_id', flat=True)
            )
            max_num = 0
            for hid in existing_ids:
                if isinstance(hid, str) and hid.isdigit():
                    num = int(hid)
                    if num > max_num:
                        max_num = num
            self.hierarchical_id = str(max_num + 1)
        
        elif self.role == 'pg_staff':
            if not self.pg_admin:
                raise ValueError("PG Staff must have a pg_admin assigned")
            if not self.pg_admin.hierarchical_id:
                # Ensure parent has an ID
                raise ValueError("Assigned pg_admin does not have a hierarchical_id yet")

            prefix = f"{self.pg_admin.hierarchical_id}."
            existing_staff_ids = (
                User.objects
                .filter(pg_admin=self.pg_admin, role='pg_staff', hierarchical_id__startswith=prefix)
                .exclude(hierarchical_id__isnull=True)
                .values_list('hierarchical_id', flat=True)
            )
            max_last = 0
            for hid in existing_staff_ids:
                try:
                    last_part = int(str(hid).split('.')[-1])
                    if last_part > max_last:
                        max_last = last_part
                except (ValueError, IndexError):
                    continue
            self.hierarchical_id = f"{prefix}{max_last + 1}"
        
        # Fallback for any other role
        elif not self.hierarchical_id:
            existing_ids = (
                User.objects
                .exclude(hierarchical_id__isnull=True)
                .values_list('hierarchical_id', flat=True)
            )
            max_num = 0
            for hid in existing_ids:
                if isinstance(hid, str) and hid.isdigit():
                    num = int(hid)
                    if num > max_num:
                        max_num = num
            self.hierarchical_id = str(max_num + 1)

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"

    def get_full_name(self):
        """Return the full name if available; otherwise, a friendly fallback."""
        name = (self.full_name or "").strip()
        if name:
            return name
        # Fallback to email local part
        if self.email:
            return self.email.split('@')[0]
        return ""

    def get_short_name(self):
        """Return a short display name for admin headers, etc."""
        name = (self.full_name or "").strip()
        if name:
            # Use first token as short name
            return name.split()[0]
        if self.email:
            return self.email.split('@')[0]
        return ""

    def is_pg_admin(self):
        return self.role == 'pg_admin'

    def is_pg_staff(self):
        return self.role == 'pg_staff'

    def has_perm(self, perm, obj=None):
        if self.is_superuser:
            return True
        return super().has_perm(perm, obj)

    def has_module_perms(self, app_label):
        if self.is_superuser:
            return True
        return super().has_module_perms(app_label)

# ----------------------------
# Signals for dynamic syncing
# ----------------------------

# Internal flags to coordinate pre_save -> post_save actions

@receiver(pre_save, sender=User)
def user_pre_save_mark_changes(sender, instance: "User", **kwargs):
    """
    Mark whether we need to sync staff permissions (when a pg_admin's permissions changed)
    or sync a staff member's permissions when their pg_admin assignment changed.
    """
    if not instance.pk:
        # New object; for new staff, copy from pg_admin after save
        if instance.role == 'pg_staff' and instance.pg_admin_id:
            setattr(instance, "_sync_from_admin_after_save", True)
        return

    try:
        db_obj = User.objects.get(pk=instance.pk)
    except User.DoesNotExist:
        return

    # Detect pg_admin's permissions change
    if instance.role == 'pg_admin':
        old_perms = db_obj.permissions or {}
        new_perms = instance.permissions or {}
        if old_perms != new_perms:
            setattr(instance, "_propagate_permissions_to_staff", True)

    # Detect staff's pg_admin assignment change
    if instance.role == 'pg_staff':
        if db_obj.pg_admin_id != instance.pg_admin_id and instance.pg_admin_id:
            setattr(instance, "_sync_from_admin_after_save", True)


@receiver(post_save, sender=User)
def user_post_save_sync_permissions(sender, instance: "User", created, **kwargs):
    """
    Perform the actual syncing after the instance is saved.
    - If a pg_admin's permissions changed, push them to all their staff.
    - If a pg_staff has been assigned/changed pg_admin, copy admin's permissions to the staff user.
    """
    # Case 1: pg_admin permission propagation
    if instance.role == 'pg_admin' and getattr(instance, "_propagate_permissions_to_staff", False):
        admin_permissions = instance.permissions or {}

        def _update_staff():
            staff_qs = User.objects.filter(pg_admin=instance, role='pg_staff')
            staff_list = list(staff_qs)
            for staff in staff_list:
                staff.permissions = admin_permissions
            if staff_list:
                User.objects.bulk_update(staff_list, ["permissions"]) 

        transaction.on_commit(_update_staff)

    # Case 2: staff syncing from assigned pg_admin
    if instance.role == 'pg_staff' and getattr(instance, "_sync_from_admin_after_save", False):
        if instance.pg_admin_id:
            def _sync_staff_user():
                try:
                    admin = User.objects.get(pk=instance.pg_admin_id)
                except User.DoesNotExist:
                    return
                # Refresh instance to avoid stale state
                staff = User.objects.filter(pk=instance.pk).first()
                if staff is None:
                    return
                staff.permissions = admin.permissions or {}
                staff.save(update_fields=["permissions"]) 

            transaction.on_commit(_sync_staff_user)


class ActivityLog(models.Model):
    ACTION_CHOICES = (
        ("create", "Create"),
        ("update", "Update"),
        ("delete", "Delete"),
        ("login", "Login"),
        ("logout", "Logout"),
    )

    user = models.ForeignKey('User', on_delete=models.CASCADE, related_name='activity_logs')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    description = models.TextField(blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['user', '-timestamp'])
        ]

    def __str__(self):
        return f"{self.user_id} {self.action} @ {self.timestamp:%Y-%m-%d %H:%M:%S}"


def log_activity(user: 'User', action: str, description: str = '', meta: dict | None = None):
    """Safely create an activity log entry."""
    if not user:
        return
    try:
        ActivityLog.objects.create(
            user=user,
            action=action,
            description=description or '',
            meta=meta or {},
        )
    except Exception as e:
        # Avoid breaking the main flow due to logging
        import logging
        logging.getLogger(__name__).warning(f"Failed to log activity: {e}")


# ----------------------------
# Pending Registration (OTP-first)
# ----------------------------
class PendingRegistration(models.Model):
    ROLE_CHOICES = (
        ('pg_admin', 'PG Admin'),
        ('pg_staff', 'PG Staff'),
    )

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=15, blank=True, null=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='pg_admin')
    # Store already-hashed password to avoid keeping plaintext
    password_hash = models.CharField(max_length=128)

    # OTP metadata
    email_otp = models.CharField(max_length=6, null=True, blank=True)
    email_otp_expires_at = models.DateTimeField(null=True, blank=True)
    email_otp_last_sent_at = models.DateTimeField(null=True, blank=True)
    email_otp_attempts = models.IntegerField(default=0)

    # If role is pg_staff, this points to the owning PG Admin
    pg_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='pending_staff_registrations',
        limit_choices_to={'role': 'pg_admin'}
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"PendingRegistration<{self.email}>"

# ----------------------------
# Auth signal-based activities
# ----------------------------

@receiver(user_logged_in)
def on_user_login(sender, request, user, **kwargs):
    # Suppress login activity logging entirely
    return
    try:
        log_activity(user, 'login', description='User logged in', meta={
            'ip': request.META.get('REMOTE_ADDR'),
            'ua': request.META.get('HTTP_USER_AGENT', '')[:255],
        })
    except Exception:
        pass


@receiver(user_logged_out)
def on_user_logout(sender, request, user, **kwargs):
    if not user:
        return
    try:
        log_activity(user, 'logout', description='User logged out', meta={
            'ip': request.META.get('REMOTE_ADDR') if request else None,
        })
    except Exception:
        pass


# ----------------------------
# Localization Settings (per PG admin owner)
# ----------------------------
class LocalizationSettings(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="accounts_localizationsettings_owner",
        help_text="PG Admin owner of these localization settings",
    )
    timezone = models.CharField(max_length=100, default="Asia/Kolkata")
    date_format = models.CharField(max_length=32, default="dd:mm:yyyy")
    time_format = models.CharField(max_length=32, default="hh:mm:ss a")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner"],
                name="uniq_localizationsettings_owner",
            ),
        ]
        indexes = [
            models.Index(fields=["owner"], name="idx_localizationsettings_owner"),
        ]
        ordering = ["owner_id", "-created_at"]

    def __str__(self) -> str:
        return f"LocalizationSettings • Owner {self.owner_id}"