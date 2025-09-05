from __future__ import annotations
from typing import Any, Optional
from datetime import timedelta, timezone as dt_timezone
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from decimal import Decimal
from django.conf import settings

from .models import Subscription, SubscriptionPlan, Coupon, CouponRedemption


def get_owner(user):
    """Return the pg_admin owner for a user (staff -> their pg_admin; admin -> self)."""
    role = getattr(user, 'role', None)
    if role == 'pg_staff' and getattr(user, 'pg_admin_id', None):
        return user.pg_admin
    return user


def get_current_subscription(owner) -> Optional[Subscription]:
    return (
        Subscription.objects.select_related('plan').filter(owner=owner, is_current=True).first()
    )


def subscription_is_valid(sub: Subscription) -> bool:
    """Return True only for active subscriptions that are not expired.
    If current_period_end is missing, compute it from start and billing interval.
    Any error results in a conservative False (treat as invalid).
    """
    try:
        if not sub:
            return False
        status_lc = (getattr(sub, 'status', '') or '').lower()
        if status_lc != 'active':
            return False
        end = getattr(sub, 'current_period_end', None)
        if not end:
            end = compute_period_end(getattr(sub, 'current_period_start'), getattr(sub, 'billing_interval', '1m'))
        now = timezone.now()
        return end is None or end > now
    except Exception:
        return False


def has_feature(user, feature_key: str) -> bool:
    owner = get_owner(user)
    sub = get_current_subscription(owner)
    if not sub or not subscription_is_valid(sub):
        return False
    # Prefer subscription-level overrides (e.g., trial features) over plan defaults
    features = None
    try:
        if isinstance(sub.meta, dict):
            cand = sub.meta.get('features')
            if isinstance(cand, dict):
                features = cand
    except Exception:
        features = None
    if features is None:
        features = sub.plan.features or {}
    return bool(features.get(feature_key, False))


def get_limit(user, limit_key: str, default: int | None = None) -> Optional[int]:
    owner = get_owner(user)
    sub = get_current_subscription(owner)
    if not sub or not subscription_is_valid(sub):
        return default
    # Prefer subscription-level overrides (e.g., trial limits) over plan defaults
    limits = None
    try:
        if isinstance(sub.meta, dict):
            cand = sub.meta.get('limits')
            if isinstance(cand, dict):
                limits = cand
    except Exception:
        limits = None
    if limits is None:
        limits = sub.plan.limits or {}
    value = limits.get(limit_key, default)
    try:
        return int(value) if value is not None else None
    except Exception:
        return default


def ensure_feature(user, feature_key: str):
    if not has_feature(user, feature_key):
        raise PermissionDenied(f"Your subscription does not include '{feature_key}'.")


def ensure_limit_not_exceeded(user, limit_key: str, used_count: int):
    limit = get_limit(user, limit_key)
    if limit is not None and used_count >= limit:
        raise ValidationError({
            'detail': f"Subscription limit reached for '{limit_key}' (used {used_count} of {limit})."
        })


# Interval utilities
def interval_days(code: str | None) -> int:
    """
    Convert an interval code to days, using 28-day months dynamically.
    Examples:
      '1m' -> 28
      '3m' -> 84
      '6m' -> 168
      '12m' -> 336
      '30d' -> 30
    Fallback for unknown/empty -> 28
    """
    if not code:
        return 28
    c = str(code).strip().lower()
    # Support day-based codes like '30d'
    if c.endswith('d'):
        try:
            return max(1, int(c[:-1]))
        except Exception:
            return 28
    # Normalize some common legacy labels
    alias = {
        'monthly': '1m', 'month': '1m', '1mo': '1m', '1month': '1m',
        'yearly': '12m', 'annual': '12m', 'annually': '12m', '12mo': '12m', '12month': '12m', '12months': '12m',
    }
    c = alias.get(c, c)
    # Default month-based codes: '<N>m'
    if c.endswith('m'):
        try:
            n = int(c[:-1] or '1')
        except Exception:
            n = 1
        return max(1, n) * 28
    return 28


def compute_period_end(start, interval_code: str | None):
    """Return period end as end-of-day after adding interval days.

    Adds interval days to the start (using 28-day months for month-based codes),
    then returns the end-of-day (23:59:59.999999) for that resulting date. The
    end-of-day is computed in the current timezone and converted to UTC for storage.

    Always returns an aware UTC datetime and handles naive inputs safely.
    """
    days = interval_days(interval_code)
    end = start + timedelta(days=days)

    tz = timezone.get_current_timezone()
    # Make sure 'end' is aware in current tz
    if timezone.is_naive(end):
        end = timezone.make_aware(end, tz)

    # Convert to local tz and snap to end-of-day
    local_end = timezone.localtime(end, tz)
    eod_local = local_end.replace(hour=23, minute=59, second=59, microsecond=999999)

    # Return as UTC aware
    return eod_local.astimezone(dt_timezone.utc)


# Pricing helpers
def price_for_plan(plan: SubscriptionPlan, interval: str | None) -> tuple[Decimal, str] | None:
    """Return (amount, currency) for a plan and interval, or None if unavailable.
    Uses plan.prices map primarily; falls back to monthly/yearly or derivation by months.
    """
    if not plan:
        return None
    currency = plan.currency or 'INR'
    code = (interval or '').strip().lower()
    if plan.prices and code in (plan.prices or {}):
        try:
            return Decimal(str(plan.prices[code])), currency
        except Exception:
            pass
    if code == '1m' and plan.price_monthly is not None:
        return Decimal(str(plan.price_monthly)), currency
    if code == '12m' and plan.price_yearly is not None:
        return Decimal(str(plan.price_yearly)), currency
    # derive months * monthly if available
    if code.endswith('m') and plan.price_monthly is not None:
        try:
            months = int(code[:-1] or '1')
        except Exception:
            months = 1
        return Decimal(str(plan.price_monthly)) * months, currency
    return None


# Tax helpers (GST)
def get_gst_percent() -> Decimal:
    """Return GST percent as a Decimal. Defaults to 18 if not configured.
    Settings override: SUBSCRIPTION_GST_PERCENT (e.g., Decimal('18')).
    """
    try:
        val = getattr(settings, 'SUBSCRIPTION_GST_PERCENT', Decimal('18'))
        return Decimal(str(val))
    except Exception:
        return Decimal('18')


def apply_gst(amount: Decimal, percent: Decimal | int | float | str | None = None) -> tuple[Decimal, Decimal]:
    """Return (gross_amount, gst_amount) where gross = amount + gst.
    Rounds to 2 decimals. Negative inputs are clamped to zero.
    """
    net = Decimal(amount or 0)
    if net < 0:
        net = Decimal('0.00')
    pct = get_gst_percent() if percent is None else Decimal(str(percent))
    if pct < 0:
        pct = Decimal('0')
    gst = (net * pct / Decimal('100')).quantize(Decimal('0.01'))
    gross = (net + gst).quantize(Decimal('0.01'))
    return gross, gst


# Coupon helpers
def get_coupon_by_code(code: str) -> Optional[Coupon]:
    if not code:
        return None
    c = str(code).strip()
    if not c:
        return None
    try:
        coupon = Coupon.objects.get(code__iexact=c)
        # Auto-deactivate if expired
        try:
            now = timezone.now()
            if coupon.is_active and coupon.valid_until and now > coupon.valid_until:
                coupon.is_active = False
                coupon.save(update_fields=['is_active', 'updated_at'])
        except Exception:
            # Best-effort; avoid blocking the read path on any failure
            pass
        return coupon
    except Coupon.DoesNotExist:
        return None


def validate_coupon_for(owner, plan: SubscriptionPlan, interval: str, coupon: Coupon):
    # Ensure coupon object exists first
    if not coupon:
        raise ValidationError({'detail': 'Invalid or inactive coupon.'})

    now = timezone.now()

    # Not yet valid window
    if coupon.valid_from and now < coupon.valid_from:
        raise ValidationError({'detail': 'Coupon is not yet valid.'})

    # Expired window: auto-deactivate best-effort, then report explicit expiry
    if coupon.valid_until and now > coupon.valid_until:
        try:
            if coupon.is_active:
                coupon.is_active = False
                coupon.save(update_fields=['is_active', 'updated_at'])
        except Exception:
            pass
        raise ValidationError({'detail': 'Coupon has expired.'})

    # After date checks, ensure active flag
    if not coupon.is_active:
        raise ValidationError({'detail': 'Invalid or inactive coupon.'})

    # Targeting
    allowed_plans = coupon.allowed_plan_slugs or []
    if allowed_plans and plan.slug not in allowed_plans:
        raise ValidationError({'detail': 'Coupon not applicable to this plan.'})
    allowed_intervals = coupon.allowed_intervals or []
    iv = (interval or '').strip().lower()
    if allowed_intervals and iv not in allowed_intervals:
        raise ValidationError({'detail': 'Coupon not applicable to this billing interval.'})

    # Usage limits
    if coupon.max_redemptions is not None:
        used = coupon.redemptions.count()
        if used >= coupon.max_redemptions:
            raise ValidationError({'detail': 'Coupon usage limit reached.'})
    if coupon.per_owner_limit is not None:
        per_used = coupon.redemptions.filter(owner=owner).count()
        if per_used >= coupon.per_owner_limit:
            raise ValidationError({'detail': 'You have already used this coupon the maximum allowed times.'})


def apply_discount(amount: Decimal, currency: str, coupon: Coupon) -> tuple[Decimal, Decimal]:
    """Return (final_amount, discount_amount).
    For percent: clamp between 0 and 100. For fixed amount: require same currency.
    Never returns a negative final amount.
    """
    amt = Decimal(amount)
    if coupon.discount_type == 'percent':
        try:
            pct = Decimal(coupon.value)
        except Exception:
            pct = Decimal('0')
        if pct < 0:
            pct = Decimal('0')
        if pct > 100:
            pct = Decimal('100')
        discount = (amt * pct / Decimal('100')).quantize(Decimal('0.01'))
    else:
        # fixed amount
        if (coupon.currency or '').upper() != (currency or '').upper():
            raise ValidationError({'detail': 'Coupon currency mismatch.'})
        try:
            discount = Decimal(coupon.value)
        except Exception:
            discount = Decimal('0')
        if discount < 0:
            discount = Decimal('0')
    final_amt = amt - discount
    if final_amt < 0:
        final_amt = Decimal('0')
    return final_amt.quantize(Decimal('0.01')), discount.quantize(Decimal('0.01'))


# Plan-level discount helpers
def plan_discount_applicable(plan: SubscriptionPlan, interval: str | None, now=None) -> bool:
    if not plan or not getattr(plan, 'discount_active', False):
        return False
    now = now or timezone.now()
    try:
        if plan.discount_valid_from and now < plan.discount_valid_from:
            return False
        if plan.discount_valid_until and now > plan.discount_valid_until:
            return False
        allowed = getattr(plan, 'discount_allowed_intervals', None) or []
        iv = (interval or '').strip().lower()
        if allowed and iv not in allowed:
            return False
        return True
    except Exception:
        return False


def apply_plan_discount(amount: Decimal, currency: str, plan: SubscriptionPlan, interval: str | None) -> tuple[Decimal, Decimal]:
    """Return (final_amount, discount_amount) applying plan-level discount if applicable.

    - Percent type: clamps 0..100
    - Amount type: applies only when plan.discount_currency matches currency (case-insensitive)
    - Never returns negative final amount
    - If not applicable/misconfigured, returns (amount, 0)
    """
    amt = Decimal(amount or 0)
    if not plan_discount_applicable(plan, interval):
        return amt.quantize(Decimal('0.01')), Decimal('0.00')
    dtype = getattr(plan, 'discount_type', 'percent') or 'percent'
    try:
        dval = Decimal(getattr(plan, 'discount_value', 0) or 0)
    except Exception:
        dval = Decimal('0')
    if dtype == 'percent':
        if dval < 0:
            dval = Decimal('0')
        if dval > 100:
            dval = Decimal('100')
        discount = (amt * dval / Decimal('100')).quantize(Decimal('0.01'))
    else:
        # fixed amount
        plan_cur = (getattr(plan, 'discount_currency', '') or '').upper()
        if plan_cur and plan_cur != (currency or '').upper():
            # currency mismatch -> do not apply
            return amt.quantize(Decimal('0.01')), Decimal('0.00')
        discount = dval
        if discount < 0:
            discount = Decimal('0')
        discount = discount.quantize(Decimal('0.01'))
    final_amt = amt - discount
    if final_amt < 0:
        final_amt = Decimal('0')
    return final_amt.quantize(Decimal('0.01')), discount.quantize(Decimal('0.01'))

# ---------------- Subscription limits helpers (bookings media) ----------------
from django.core.exceptions import PermissionDenied

def _get_current_subscription_and_limits(user):
    """
    Return (subscription, limits_dict) for the current owner.
    If no current subscription, returns (None, {}).
    """
    from .models import Subscription
    sub = (
        Subscription.objects
        .select_related('plan')
        .filter(owner=user, is_current=True)
        .first()
    )
    return sub, (getattr(getattr(sub, 'plan', None), 'limits', None) or {})

def get_limit_value(limits: dict, dotted_key: str, default=None):
    """Fetch nested limit value like 'bookings_media.max_file_bytes' from a dict."""
    node = limits or {}
    for part in str(dotted_key).split('.'):
        if not isinstance(node, dict):
            return default
        if part not in node:
            return default
        node = node.get(part)
    return default if node is None else node

def enforce_booking_media_upload_limits(user, booking, file_obj, current_file_count: int, current_total_bytes: int):
    """
    Enforce bookings media limits from subscription plan:
    - bookings_media.allowed_mime_prefixes (comma-separated): e.g. 'image/,application/pdf'
    - bookings_media.max_file_bytes
    - bookings_media.max_files_per_booking
    - bookings_media.max_total_bytes_per_booking
    - storage_mb (global cap, optional)
    Raises PermissionDenied with a user-safe message if a limit is exceeded.
    """
    from django.db.models import Sum

    sub, limits = _get_current_subscription_and_limits(user)
    if not sub:
        raise PermissionDenied('No active subscription')

    # Allowed MIME types by prefix
    allowed = get_limit_value(limits, 'bookings_media.allowed_mime_prefixes', default=None)
    if allowed:
        prefixes = [p.strip().lower() for p in str(allowed).split(',') if p.strip()]
        mime = (getattr(file_obj, 'content_type', '') or '').lower()
        if mime and prefixes and not any(mime.startswith(p) for p in prefixes):
            raise PermissionDenied('File type not allowed for your plan')

    # Per-file size
    max_file_bytes = get_limit_value(limits, 'bookings_media.max_file_bytes', default=None)
    file_size = getattr(file_obj, 'size', None)
    if max_file_bytes is not None and file_size is not None and int(file_size) > int(max_file_bytes):
        raise PermissionDenied('File exceeds per-file size limit')

    # Per-booking file count
    max_files = get_limit_value(limits, 'bookings_media.max_files_per_booking', default=None)
    if max_files is not None and int(current_file_count) + 1 > int(max_files):
        raise PermissionDenied('You have reached the maximum number of files for this booking')

    # Per-booking total bytes
    max_total = get_limit_value(limits, 'bookings_media.max_total_bytes_per_booking', default=None)
    if max_total is not None and file_size is not None and int(current_total_bytes) + int(file_size) > int(max_total):
        raise PermissionDenied('This upload exceeds your per-booking storage limit')

    # Global storage cap in MB (optional)
    storage_mb = get_limit_value(limits, 'storage_mb', default=None)
    if storage_mb is not None and file_size is not None:
        try:
            from bookings.models import BookingMedia
            used_bytes = (BookingMedia.objects
                          .filter(owner=user)
                          .aggregate(s=Sum('file_size'))
                          .get('s') or 0)
            if int(used_bytes) + int(file_size) > int(storage_mb) * 1024 * 1024:
                raise PermissionDenied('You have reached your plan storage limit')
        except Exception:
            # If the aggregate fails for any reason, do not hard-block here
            pass

    return True
