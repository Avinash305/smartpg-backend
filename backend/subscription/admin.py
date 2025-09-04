from django.contrib import admin
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
import re

from .models import SubscriptionPlan, Subscription, Coupon, CouponRedemption


class SubscriptionPlanAdminForm(forms.ModelForm):
    # Features as checkboxes + optional new keys
    features_choices = forms.MultipleChoiceField(
        required=False,
        label='Features (enable by checking)',
        help_text='Tick to enable features. Use the field below to add new feature keys.',
        choices=(),
        widget=forms.CheckboxSelectMultiple,
    )
    features_new_keys = forms.CharField(
        required=False,
        label='Add new feature keys',
        help_text='Comma or space-separated keys. Newly added keys will be enabled.',
        widget=forms.TextInput(attrs={
            'placeholder': 'priority_support analytics export_data',
            'class': 'vTextField',
        }),
    )
    # Limits expects integer values or blank for unlimited
    limits_kv = forms.CharField(
        required=False,
        label='Advanced limits',
        help_text='',
        widget=forms.Textarea(attrs={
            'rows': 5,
            'class': 'vLargeTextField monospace',
            'placeholder': 'rooms=10\nstaff='
        }),
    )
    # User-friendly common limits: Unlimited checkbox or numeric count
    limit_buildings_unlimited = forms.BooleanField(required=False, label='Buildings: Unlimited')
    limit_buildings_count = forms.IntegerField(required=False, min_value=0, label='Buildings: Count')
    limit_staff_unlimited = forms.BooleanField(required=False, label='Staff: Unlimited')
    limit_staff_count = forms.IntegerField(required=False, min_value=0, label='Staff: Count')
    limit_tenants_unlimited = forms.BooleanField(required=False, label='Tenants: Unlimited')
    limit_tenants_count = forms.IntegerField(required=False, min_value=0, label='Tenants: Count')
    limit_storage_mb_unlimited = forms.BooleanField(required=False, label='Storage (MB): Unlimited')
    limit_storage_mb_count = forms.IntegerField(required=False, min_value=0, label='Storage (MB): Count')
    available_intervals = forms.MultipleChoiceField(
        required=False,
        choices=(('1m', '1 month'), ('3m', '3 months'), ('6m', '6 months'), ('12m', '12 months')),
        initial=['1m', '3m', '6m', '12m'],
        help_text="Allowed billing intervals for this plan",
        widget=forms.CheckboxSelectMultiple,
    )
    # User-friendly per-interval price inputs (mapped into model 'prices' JSON)
    price_1m = forms.DecimalField(required=False, min_value=0, max_digits=10, decimal_places=2,
                                  label='Price (1 month)',
                                  widget=forms.NumberInput(attrs={'placeholder': 'e.g. 499.00'}))
    price_3m = forms.DecimalField(required=False, min_value=0, max_digits=10, decimal_places=2,
                                  label='Price (3 months)',
                                  widget=forms.NumberInput(attrs={'placeholder': 'e.g. 1299.00'}))
    price_6m = forms.DecimalField(required=False, min_value=0, max_digits=10, decimal_places=2,
                                  label='Price (6 months)',
                                  widget=forms.NumberInput(attrs={'placeholder': 'e.g. 2399.00'}))
    price_12m = forms.DecimalField(required=False, min_value=0, max_digits=10, decimal_places=2,
                                   label='Price (12 months / 1 year)',
                                   widget=forms.NumberInput(attrs={'placeholder': 'e.g. 4499.00'}))

    # Plan-level discount configuration
    discount_active = forms.BooleanField(required=False, label='Enable discount')
    discount_type = forms.ChoiceField(
        required=False,
        choices=(("percent", "Percent"), ("amount", "Fixed Amount")),
        initial="percent",
        label='Discount type',
    )
    discount_value = forms.DecimalField(required=False, min_value=0, max_digits=10, decimal_places=2, label='Discount value')
    discount_currency = forms.CharField(required=False, max_length=10, initial='INR', label='Discount currency (for amount)')
    discount_valid_from = forms.DateTimeField(required=False, label='Discount valid from')
    discount_valid_until = forms.DateTimeField(required=False, label='Discount valid until')
    discount_allowed_intervals = forms.MultipleChoiceField(
        required=False,
        choices=(('1m', '1 month'), ('3m', '3 months'), ('6m', '6 months'), ('12m', '12 months')),
        help_text="Intervals where discount applies (empty = all)",
        widget=forms.CheckboxSelectMultiple,
    )
    discount_description = forms.CharField(required=False, max_length=255, label='Discount description')

    class Meta:
        model = SubscriptionPlan
        fields = [
            'name', 'slug', 'currency', 'price_monthly', 'price_yearly',
            'is_active', 'features', 'limits', 'available_intervals', 'prices',
            # Discount fields
            'discount_active', 'discount_type', 'discount_value', 'discount_currency',
            'discount_valid_from', 'discount_valid_until', 'discount_allowed_intervals', 'discount_description',
        ]
        widgets = {
            'prices': forms.HiddenInput(),  # hide raw JSON; built from price_* fields
            'features': forms.HiddenInput(),  # built from features_kv
            'limits': forms.HiddenInput(),    # built from limits_kv
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Pre-fill user-friendly price fields from model 'prices'
        pm = (self.instance.prices or {}) if getattr(self.instance, 'pk', None) else {}
        self.fields['price_1m'].initial = pm.get('1m')
        self.fields['price_3m'].initial = pm.get('3m')
        self.fields['price_6m'].initial = pm.get('6m')
        self.fields['price_12m'].initial = pm.get('12m')
        # Build features checkbox choices from all plans + current instance
        catalog = set()
        try:
            for d in SubscriptionPlan.objects.values_list('features', flat=True):
                if isinstance(d, dict):
                    catalog.update(list(d.keys()))
        except Exception:
            pass
        current_feats = getattr(self.instance, 'features', None) or {}
        catalog.update(list(current_feats.keys()))
        choices = [(k, k.replace('_', ' ').title()) for k in sorted(catalog)]
        self.fields['features_choices'].choices = choices
        self.fields['features_choices'].initial = [k for k, v in current_feats.items() if v]
        # Pre-fill limits KV from model dict
        lims = getattr(self.instance, 'limits', None) or {}
        if isinstance(lims, dict):
            lines = []
            for k, v in lims.items():
                lines.append(f"{k}={'' if v is None else v}")
            self.fields['limits_kv'].initial = "\n".join(lines)
        # Pre-fill common limit fields from model limits
        def init_limit_pair(key: str, unlimited_field: str, count_field: str):
            v = lims.get(key, None)
            if v is None and key in lims:
                self.fields[unlimited_field].initial = True
                self.fields[count_field].initial = None
            elif v is not None and key in lims:
                try:
                    self.fields[count_field].initial = int(v)
                except Exception:
                    self.fields[count_field].initial = None
        init_limit_pair('buildings', 'limit_buildings_unlimited', 'limit_buildings_count')
        init_limit_pair('staff', 'limit_staff_unlimited', 'limit_staff_count')
        init_limit_pair('tenants', 'limit_tenants_unlimited', 'limit_tenants_count')
        init_limit_pair('storage_mb', 'limit_storage_mb_unlimited', 'limit_storage_mb_count')
        # Pre-fill discount fields from instance
        inst = self.instance
        if getattr(inst, 'pk', None):
            self.fields['discount_active'].initial = getattr(inst, 'discount_active', False)
            self.fields['discount_type'].initial = getattr(inst, 'discount_type', 'percent')
            self.fields['discount_value'].initial = getattr(inst, 'discount_value', 0)
            self.fields['discount_currency'].initial = getattr(inst, 'discount_currency', 'INR')
            self.fields['discount_valid_from'].initial = getattr(inst, 'discount_valid_from', None)
            self.fields['discount_valid_until'].initial = getattr(inst, 'discount_valid_until', None)
            dai = getattr(inst, 'discount_allowed_intervals', None) or []
            self.fields['discount_allowed_intervals'].initial = [i for i in dai if i in {'1m','3m','6m','12m'}]
            self.fields['discount_description'].initial = getattr(inst, 'discount_description', '')

    def clean(self):
        cleaned = super().clean()

        # Normalize currency to uppercase 3-10 chars (leave as-is if blank)
        currency = cleaned.get('currency')
        if currency:
            cleaned['currency'] = str(currency).upper()
        # Normalize discount currency
        dcur = cleaned.get('discount_currency')
        if dcur:
            cleaned['discount_currency'] = str(dcur).upper()

        # Validate prices are non-negative
        for field in ('price_monthly', 'price_yearly'):
            price = cleaned.get(field)
            if price is not None and price < 0:
                self.add_error(field, 'Price cannot be negative')

        # Parse Limits KV -> dict[str,int|None]
        limits_kv = (cleaned.get('limits_kv') or '').strip()
        limits: dict[str, int | None] = {}
        if limits_kv:
            for raw_line in limits_kv.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if '=' not in line:
                    raise ValidationError({'limits_kv': f"Invalid line '{line}'. Expected 'key=number' or 'key=' for unlimited"})
                key, val = [p.strip() for p in line.split('=', 1)]
                if not key:
                    raise ValidationError({'limits_kv': 'Limit key cannot be empty'})
                if val == '':
                    limits[key] = None
                    continue
                try:
                    iv = int(val)
                except Exception:
                    raise ValidationError({'limits_kv': f"Limit '{key}' must be an integer or blank for unlimited"})
                if iv < 0:
                    raise ValidationError({'limits_kv': f"Limit '{key}' cannot be negative"})
                limits[key] = iv
        # Override with user-friendly common limit fields
        def apply_pair(key: str, unlimited_field: str, count_field: str, label: str):
            if cleaned.get(unlimited_field):
                limits[key] = None
                return
            cnt = cleaned.get(count_field)
            if cnt not in (None, ''):
                try:
                    iv = int(cnt)
                except Exception:
                    raise ValidationError({count_field: f"{label} must be an integer"})
                if iv < 0:
                    raise ValidationError({count_field: f"{label} cannot be negative"})
                limits[key] = iv
        apply_pair('buildings', 'limit_buildings_unlimited', 'limit_buildings_count', 'Buildings')
        apply_pair('staff', 'limit_staff_unlimited', 'limit_staff_count', 'Staff')
        apply_pair('tenants', 'limit_tenants_unlimited', 'limit_tenants_count', 'Tenants')
        apply_pair('storage_mb', 'limit_storage_mb_unlimited', 'limit_storage_mb_count', 'Storage (MB)')
        cleaned['limits'] = limits

        # Validate available_intervals: subset of supported values
        intervals = cleaned.get('available_intervals') or ['1m', '3m', '6m', '12m']
        if not isinstance(intervals, (list, tuple)):
            intervals = ['1m', '3m', '6m', '12m']
        allowed_values = {'1m', '3m', '6m', '12m'}
        intervals = [i for i in intervals if i in allowed_values]
        if not intervals:
            intervals = ['1m', '3m', '6m', '12m']
        cleaned['available_intervals'] = intervals

        # Build prices JSON from user-friendly fields and validate
        price_map = {}
        for code, field in (('1m', 'price_1m'), ('3m', 'price_3m'), ('6m', 'price_6m'), ('12m', 'price_12m')):
            val = cleaned.get(field)
            if val in (None, ''):
                continue
            try:
                amt = float(val)
            except Exception:
                raise ValidationError({'prices': f"Price for '{code}' must be numeric"})
            if amt < 0:
                raise ValidationError({'prices': f"Price for '{code}' cannot be negative"})
            price_map[code] = amt
        cleaned['prices'] = price_map

        # Validate discount fields
        if cleaned.get('discount_active'):
            dtype = cleaned.get('discount_type') or 'percent'
            dval = cleaned.get('discount_value')
            if dval is None:
                dval = 0
            if dtype == 'percent' and dval and dval > 100:
                self.add_error('discount_value', 'Percent discount cannot exceed 100')
            # Sanitize allowed intervals
            intervals = cleaned.get('discount_allowed_intervals') or []
            if not isinstance(intervals, (list, tuple)):
                intervals = []
            allowed_values = {'1m','3m','6m','12m'}
            cleaned['discount_allowed_intervals'] = [i for i in intervals if i in allowed_values]

        # Build Features dict from checkboxes + new keys
        selected = set(cleaned.get('features_choices') or [])
        new_keys_raw = (cleaned.get('features_new_keys') or '').strip()
        new_keys = set()
        if new_keys_raw:
            for part in re.split(r'[\s,]+', new_keys_raw):
                k = part.strip()
                if k:
                    new_keys.add(k)
        prior_keys = set((getattr(self.instance, 'features', None) or {}).keys())
        all_keys = prior_keys | new_keys | selected
        features = {k: (k in selected) for k in all_keys}
        cleaned['features'] = features

        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        # Ensure model 'prices' JSON is updated from cleaned form data
        instance.prices = self.cleaned_data.get('prices') or {}
        # Ensure features/limits saved from KV inputs
        instance.features = self.cleaned_data.get('features') or {}
        instance.limits = self.cleaned_data.get('limits') or {}
        if commit:
            instance.save()
        return instance


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    form = SubscriptionPlanAdminForm

    list_display = (
        'name', 'slug', 'currency', 'price_monthly', 'price_yearly', 'is_active',
        'price_summary', 'features_summary', 'limits_summary', 'created_at',
    )
    list_filter = ('is_active', 'currency')
    search_fields = ('name', 'slug')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ('is_active',)

    fieldsets = (
        ('General', {
            'fields': ('name', 'slug', 'currency', 'is_active'),
        }),
        ('Legacy pricing (fallbacks)', {
            'fields': ('price_monthly', 'price_yearly'),
            'classes': ('collapse',),
            'description': 'Optional legacy fields used as fallbacks if flexible prices are not set.',
        }),
        ('Flexible billing', {
            'fields': ('available_intervals', 'price_1m', 'price_3m', 'price_6m', 'price_12m'),
            'description': 'Choose allowed intervals and set per-interval prices.',
        }),
        ('Plan discount (auto-applied before coupons)', {
            'fields': (
                'discount_active', 'discount_type', 'discount_value', 'discount_currency',
                'discount_valid_from', 'discount_valid_until', 'discount_allowed_intervals', 'discount_description',
            ),
            'description': 'Configure optional plan-level discount. For percent, value is 0-100. For fixed amount, currency must match plan currency at checkout.',
        }),
        ('Features & Limits', {
            'fields': (
                'features_choices', 'features_new_keys',
                'limit_buildings_unlimited', 'limit_buildings_count',
                'limit_staff_unlimited', 'limit_staff_count',
                'limit_tenants_unlimited', 'limit_tenants_count',
                'limit_storage_mb_unlimited', 'limit_storage_mb_count',
                'limits_kv',
            ),
            'description': 'Tick features to enable. To add new feature keys, type them separated by spaces/commas. For limits, use key=value per line; leave value blank for unlimited.',
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    def price_summary(self, obj: SubscriptionPlan):
        pm = obj.prices or {}
        parts = []
        for code, label in (('1m', '1m'), ('3m', '3m'), ('6m', '6m'), ('12m', '12m')):
            val = pm.get(code)
            if val is not None:
                parts.append(f"{label}: {val}")
        return ", ".join(parts) if parts else "—"

    price_summary.short_description = 'Prices'

    def features_summary(self, obj: SubscriptionPlan):
        feats = obj.features or {}
        enabled = sorted([k for k, v in feats.items() if v])
        return ", ".join(enabled) if enabled else "—"

    features_summary.short_description = 'Enabled features'

    def limits_summary(self, obj: SubscriptionPlan):
        lim = obj.limits or {}
        if not lim:
            return "—"
        parts = []
        for k in sorted(lim.keys()):
            v = lim[k]
            parts.append(f"{k}:{v if v is not None else '∞'}")
        return ", ".join(parts)

    limits_summary.short_description = 'Limits'


class SubscriptionAdminForm(forms.ModelForm):
    class Meta:
        model = Subscription
        fields = '__all__'

    def clean(self):
        cleaned = super().clean()
        owner = cleaned.get('owner')
        is_current = cleaned.get('is_current')
        instance_id = self.instance.pk if self.instance and self.instance.pk else None
        if owner and is_current:
            # Enforce single current subscription per owner
            existing = Subscription.objects.filter(owner=owner, is_current=True)
            if instance_id:
                existing = existing.exclude(pk=instance_id)
            if existing.exists():
                raise ValidationError({
                    'is_current': 'Only one current subscription is allowed per owner.'
                })
        return cleaned


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    form = SubscriptionAdminForm

    list_display = (
        'owner', 'plan', 'status', 'billing_interval', 'is_current', 'cancel_at_period_end',
        'current_period_start', 'current_period_end', 'trial_end', 'created_at',
    )
    list_filter = (
        'status', 'is_current', 'cancel_at_period_end', 'plan',
    )
    search_fields = (
        'owner__email', 'owner__username', 'plan__name', 'plan__slug'
    )
    autocomplete_fields = ('owner', 'plan')
    list_select_related = ('owner', 'plan')
    date_hierarchy = 'created_at'
    ordering = ('owner_id', '-created_at')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = (
        'code', 'discount_type', 'value', 'currency', 'is_active',
        'valid_from', 'valid_until', 'max_redemptions', 'per_owner_limit', 'created_at',
    )
    list_filter = ('is_active', 'discount_type')
    search_fields = ('code', 'description')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(CouponRedemption)
class CouponRedemptionAdmin(admin.ModelAdmin):
    list_display = ('coupon', 'owner', 'subscription', 'redeemed_at')
    search_fields = ('coupon__code', 'owner__email', 'owner__username')
    list_filter = ('redeemed_at',)
    autocomplete_fields = ('coupon', 'owner', 'subscription')
    list_select_related = ('coupon', 'owner', 'subscription')
    date_hierarchy = 'redeemed_at'
    ordering = ('-redeemed_at',)