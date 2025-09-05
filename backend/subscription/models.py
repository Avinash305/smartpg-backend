from django.db import models
from django.conf import settings
from django.utils import timezone
from django.db.models import Q


def default_intervals():
    # Support flexible terms in months
    return ["1m", "3m", "6m", "12m"]


class SubscriptionPlan(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    currency = models.CharField(max_length=10, default='INR')
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price_yearly = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    # Dynamic capability flags and limits (editable without code changes)
    # Example:
    # features = {"bookings": true, "payments": true, "reports": false}
    # limits = {"buildings": 3, "staff": 5, "tenants": 100, "storage_mb": 2048}
    features = models.JSONField(default=dict, blank=True)
    limits = models.JSONField(default=dict, blank=True)
    available_intervals = models.JSONField(
        default=default_intervals,
        blank=True,
        help_text="Allowed billing intervals codes e.g. ['1m','3m','6m','12m']",
    )
    prices = models.JSONField(
        default=dict,
        blank=True,
        help_text="Map of interval code to price, e.g. {'1m': 499, '3m': 1299, '6m': 2399, '12m': 4499}"
    )

    # Optional plan-level discount configuration (auto-applied before coupons)
    discount_active = models.BooleanField(default=False)
    discount_type = models.CharField(
        max_length=16,
        choices=(
            ("percent", "Percent"),
            ("amount", "Fixed Amount"),
        ),
        default="percent",
    )
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_currency = models.CharField(max_length=10, default='INR', help_text="Used for fixed amount discounts")
    discount_valid_from = models.DateTimeField(null=True, blank=True)
    discount_valid_until = models.DateTimeField(null=True, blank=True)
    discount_allowed_intervals = models.JSONField(default=list, blank=True, help_text="Empty = all intervals (e.g. '1m','12m')")
    discount_description = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["slug"], name="idx_plan_slug"),
            models.Index(fields=["is_active"], name="idx_plan_is_active"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"


class Subscription(models.Model):
    STATUS_CHOICES = (
        ("active", "Active"),
        ("past_due", "Past due"),
        ("canceled", "Canceled"),
        ("expired", "Expired"),
    )

    # Owner is the PG Admin user. For staff, we will resolve to their pg_admin in views/helpers.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        limit_choices_to={"role": "pg_admin"},
        help_text="PG Admin owner of this subscription",
    )
    plan = models.ForeignKey(
        SubscriptionPlan,
        on_delete=models.PROTECT,
        related_name="subscriptions",
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="active", db_index=True)
    # Store interval as a flexible code, e.g., '1m','3m','6m','12m'
    billing_interval = models.CharField(max_length=10, default="1m")

    current_period_start = models.DateTimeField(default=timezone.now)
    current_period_end = models.DateTimeField(null=True, blank=True)
    trial_end = models.DateTimeField(null=True, blank=True)

    cancel_at_period_end = models.BooleanField(default=False)
    is_current = models.BooleanField(default=True, db_index=True)

    meta = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["owner_id", "-created_at"]
        indexes = [
            models.Index(fields=["owner", "status"], name="idx_sub_owner_status"),
            models.Index(fields=["owner", "-created_at"], name="idx_sub_owner_created"),
        ]
        constraints = [
            # Ensure at most one current subscription per owner
            models.UniqueConstraint(
                fields=["owner"],
                condition=Q(is_current=True),
                name="uniq_current_subscription_per_owner",
            )
        ]

    def __str__(self) -> str:
        return f"Subscription<{self.owner_id}:{self.plan.slug}:{self.status}>"


class Coupon(models.Model):
    DISCOUNT_TYPES = (
        ("percent", "Percent"),
        ("amount", "Fixed Amount"),
    )

    code = models.CharField(max_length=64, unique=True, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    discount_type = models.CharField(max_length=16, choices=DISCOUNT_TYPES, default="percent")
    value = models.DecimalField(max_digits=10, decimal_places=2, help_text="Percent (0-100) or fixed amount depending on type")
    currency = models.CharField(max_length=10, default='INR', help_text="Used for fixed amount discounts")

    # Validity window
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)

    # Usage limits
    max_redemptions = models.PositiveIntegerField(null=True, blank=True, help_text="Global max uses (blank = unlimited)")
    per_owner_limit = models.PositiveIntegerField(null=True, blank=True, help_text="Max uses per PG owner (blank = unlimited)")

    # Targeting
    allowed_plan_slugs = models.JSONField(default=list, blank=True, help_text="Empty = all plans")
    allowed_intervals = models.JSONField(default=list, blank=True, help_text="Empty = all intervals (e.g. '1m','3m')")

    is_active = models.BooleanField(default=True)
    meta = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["code"], name="idx_coupon_code"),
            models.Index(fields=["is_active"], name="idx_coupon_active"),
        ]

    def __str__(self) -> str:
        return f"Coupon<{self.code}>"


class CouponRedemption(models.Model):
    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name="redemptions")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="coupon_redemptions")
    subscription = models.ForeignKey(Subscription, on_delete=models.SET_NULL, null=True, blank=True, related_name="coupon_redemptions")
    redeemed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["coupon_id", "owner_id"], name="idx_coupon_owner"),
            models.Index(fields=["redeemed_at"], name="idx_coupon_redeemed_at"),
        ]

    def __str__(self) -> str:
        return f"Redemption<{self.coupon_id}:{self.owner_id}:{self.redeemed_at}>"
