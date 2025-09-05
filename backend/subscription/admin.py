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
    limit_floors_unlimited = forms.BooleanField(required=False, label='Floors: Unlimited')
    limit_floors_count = forms.IntegerField(required=False, min_value=0, label='Floors: Count')
    limit_rooms_unlimited = forms.BooleanField(required=False, label='Rooms: Unlimited')
    limit_rooms_count = forms.IntegerField(required=False, min_value=0, label='Rooms: Count')
    limit_beds_unlimited = forms.BooleanField(required=False, label='Beds: Unlimited')
    limit_beds_count = forms.IntegerField(required=False, min_value=0, label='Beds: Count')
    limit_bookings_unlimited = forms.BooleanField(required=False, label='Bookings: Unlimited')
    limit_bookings_count = forms.IntegerField(required=False, min_value=0, label='Bookings: Count')
    limit_invoices_per_month_unlimited = forms.BooleanField(required=False, label='Invoices/month: Unlimited')
    limit_invoices_per_month_count = forms.IntegerField(required=False, min_value=0, label='Invoices/month: Count')
    bm_max_files_unlimited = forms.BooleanField(required=False, label='Bookings media: Files per booking Unlimited')
    bm_max_files_count = forms.IntegerField(required=False, min_value=0, label='Bookings media: Files per booking Count')
    bm_max_file_bytes = forms.IntegerField(required=False, min_value=0, label='Bookings media: Max file size (bytes)')
    bm_max_total_bytes_per_booking = forms.IntegerField(required=False, min_value=0, label='Bookings media: Max total per booking (bytes)')
    bm_allowed_mime_prefixes = forms.CharField(required=False, label='Bookings media: Allowed MIME prefixes', help_text="Comma-separated prefixes, e.g. 'image/,application/pdf'")
    # Tenants media (per-tenant count)
    limit_tenant_media_per_tenant_unlimited = forms.BooleanField(required=False, label='Tenant media per tenant: Unlimited')
    limit_tenant_media_per_tenant_count = forms.IntegerField(required=False, min_value=0, label='Tenant media per tenant: Count')
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
        init_limit_pair('floors', 'limit_floors_unlimited', 'limit_floors_count')
        init_limit_pair('rooms', 'limit_rooms_unlimited', 'limit_rooms_count')
        init_limit_pair('beds', 'limit_beds_unlimited', 'limit_beds_count')
        init_limit_pair('bookings', 'limit_bookings_unlimited', 'limit_bookings_count')
        # Tenants media per-tenant count
        init_limit_pair('max_tenant_media_per_tenant', 'limit_tenant_media_per_tenant_unlimited', 'limit_tenant_media_per_tenant_count')
        # Nested helpers for limits like invoices.max_per_month and bookings_media.*
        def get_nested(dct: dict, dotted: str, default=None):
            node = dct
            for part in str(dotted).split('.'):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node.get(part)
            return node if node is not None else default
        # Invoices/month
        inv = get_nested(lims, 'invoices.max_per_month', default=None)
        if inv is None and 'invoices' in lims:
            self.fields['limit_invoices_per_month_unlimited'].initial = True
        elif inv is not None:
            try:
                self.fields['limit_invoices_per_month_count'].initial = int(inv)
            except Exception:
                self.fields['limit_invoices_per_month_count'].initial = None
        # Bookings media
        bm_max_files = get_nested(lims, 'bookings_media.max_files_per_booking', default=None)
        if bm_max_files is None and 'bookings_media' in lims:
            self.fields['bm_max_files_unlimited'].initial = True
        elif bm_max_files is not None:
            try:
                self.fields['bm_max_files_count'].initial = int(bm_max_files)
            except Exception:
                self.fields['bm_max_files_count'].initial = None
        self.fields['bm_max_file_bytes'].initial = get_nested(lims, 'bookings_media.max_file_bytes', default=None)
        self.fields['bm_max_total_bytes_per_booking'].initial = get_nested(lims, 'bookings_media.max_total_bytes_per_booking', default=None)
        self.fields['bm_allowed_mime_prefixes'].initial = get_nested(lims, 'bookings_media.allowed_mime_prefixes', default=None)
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
        apply_pair('floors', 'limit_floors_unlimited', 'limit_floors_count', 'Floors')
        apply_pair('rooms', 'limit_rooms_unlimited', 'limit_rooms_count', 'Rooms')
        apply_pair('beds', 'limit_beds_unlimited', 'limit_beds_count', 'Beds')
        apply_pair('bookings', 'limit_bookings_unlimited', 'limit_bookings_count', 'Bookings')
        # Tenants media per-tenant count
        apply_pair('max_tenant_media_per_tenant', 'limit_tenant_media_per_tenant_unlimited', 'limit_tenant_media_per_tenant_count', 'Tenant media per tenant')

        # Nested setter
        def set_nested(dct: dict, dotted: str, value):
            parts = str(dotted).split('.')
            node = dct
            for p in parts[:-1]:
                if p not in node or not isinstance(node[p], dict):
                    node[p] = {}
                node = node[p]
            node[parts[-1]] = value

        # Invoices/month
        if cleaned.get('limit_invoices_per_month_unlimited'):
            set_nested(limits, 'invoices.max_per_month', None)
        else:
            inv_cnt = cleaned.get('limit_invoices_per_month_count')
            if inv_cnt not in (None, ''):
                try:
                    iv = int(inv_cnt)
                except Exception:
                    raise ValidationError({'limit_invoices_per_month_count': 'Invoices/month must be an integer'})
                if iv < 0:
                    raise ValidationError({'limit_invoices_per_month_count': 'Invoices/month cannot be negative'})
                set_nested(limits, 'invoices.max_per_month', iv)

        # Bookings media
        if cleaned.get('bm_max_files_unlimited'):
            set_nested(limits, 'bookings_media.max_files_per_booking', None)
        else:
            bm_files = cleaned.get('bm_max_files_count')
            if bm_files not in (None, ''):
                try:
                    iv = int(bm_files)
                except Exception:
                    raise ValidationError({'bm_max_files_count': 'Files per booking must be an integer'})
                if iv < 0:
                    raise ValidationError({'bm_max_files_count': 'Files per booking cannot be negative'})
                set_nested(limits, 'bookings_media.max_files_per_booking', iv)

        bm_file_bytes = cleaned.get('bm_max_file_bytes')
        if bm_file_bytes not in (None, ''):
            try:
                iv = int(bm_file_bytes)
            except Exception:
                raise ValidationError({'bm_max_file_bytes': 'Max file size must be an integer (bytes)'})
            if iv < 0:
                raise ValidationError({'bm_max_file_bytes': 'Max file size cannot be negative'})
            set_nested(limits, 'bookings_media.max_file_bytes', iv)

        bm_total_bytes = cleaned.get('bm_max_total_bytes_per_booking')
        if bm_total_bytes not in (None, ''):
            try:
                iv = int(bm_total_bytes)
            except Exception:
                raise ValidationError({'bm_max_total_bytes_per_booking': 'Max total per booking must be an integer (bytes)'})
            if iv < 0:
                raise ValidationError({'bm_max_total_bytes_per_booking': 'Max total per booking cannot be negative'})
            set_nested(limits, 'bookings_media.max_total_bytes_per_booking', iv)

        mime_prefixes = (cleaned.get('bm_allowed_mime_prefixes') or '').strip()
        if mime_prefixes:
            set_nested(limits, 'bookings_media.allowed_mime_prefixes', mime_prefixes)
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
        'price_summary', 'features_summary', 'limits_summary', 'discount_window', 'created_at',
    )
    list_filter = ('is_active', 'currency')
    search_fields = ('name', 'slug')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at')
    prepopulated_fields = {"slug": ("name",)}
    list_editable = ('is_active',)
    actions = ('make_active', 'make_inactive')

    def discount_window(self, obj: SubscriptionPlan):
        try:
            if getattr(obj, 'discount_active', False):
                start = getattr(obj, 'discount_valid_from', None)
                end = getattr(obj, 'discount_valid_until', None)
                if start and end:
                    return f"{start:%Y-%m-%d} → {end:%Y-%m-%d}"
                if start:
                    return f"from {start:%Y-%m-%d}"
                if end:
                    return f"until {end:%Y-%m-%d}"
                return 'active'
            return '—'
        except Exception:
            return '—'

    discount_window.short_description = 'Discount window'

    def make_active(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f"Activated {updated} plan(s)")

    make_active.short_description = 'Activate selected plans'

    def make_inactive(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f"Deactivated {updated} plan(s)")

    make_inactive.short_description = 'Deactivate selected plans'

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
            v = lim.get(k)
            parts.append(f"{k}:{v if v is not None else '∞'}")
        return ", ".join(parts)

    limits_summary.short_description = 'Limits'


class FreeMonthFilter(admin.SimpleListFilter):
    title = 'Free month'
    parameter_name = 'free_month'

    def lookups(self, request, model_admin):
        return (
            ('yes', 'Yes'),
            ('no', 'No'),
        )

    def queryset(self, request, queryset):
        val = self.value()
        if val == 'yes':
            return queryset.filter(meta__free_month=True)
        if val == 'no':
            return queryset.exclude(meta__free_month=True)
        return queryset


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
        'current_period_start', 'current_period_end', 'is_free_month', 'free_ends_at', 'limits_preview', 'created_at',
    )
    list_filter = (
        'status', 'is_current', 'cancel_at_period_end', 'plan', 'billing_interval',
        FreeMonthFilter,
    )
    search_fields = (
        'owner__email', 'owner__username', 'plan__name', 'plan__slug'
    )
    autocomplete_fields = ('owner', 'plan')
    list_select_related = ('owner', 'plan')
    date_hierarchy = 'created_at'
    ordering = ('owner_id', '-created_at')
    readonly_fields = ('created_at', 'updated_at')
    actions = ('set_as_current', 'set_cancel_at_period_end', 'unset_cancel_at_period_end')

    def is_free_month(self, obj):
        try:
            return bool(isinstance(obj.meta, dict) and obj.meta.get('free_month'))
        except Exception:
            return False

    is_free_month.boolean = True
    is_free_month.short_description = 'Free month'

    def free_ends_at(self, obj):
        try:
            if self.is_free_month(obj):
                # Prefer current period end when available
                return obj.current_period_end
            return None
        except Exception:
            return None

    free_ends_at.short_description = 'Free ends'

    def limits_preview(self, obj):
        try:
            lims = {}
            if isinstance(obj.meta, dict):
                cand = obj.meta.get('limits')
                if isinstance(cand, dict):
                    lims = cand
            if not lims:
                return '—'
            keys = ['buildings', 'staff', 'floors', 'rooms', 'beds', 'tenants']
            parts = []
            for k in keys:
                if k in lims:
                    v = lims.get(k)
                    parts.append(f"{k}:{v if v is not None else '∞'}")
            return ", ".join(parts) if parts else '—'
        except Exception:
            return '—'

    limits_preview.short_description = 'Limits (subset)'

    def set_as_current(self, request, queryset):
        count = 0
        for sub in queryset.select_related('owner'):
            # Unset others for this owner
            Subscription.objects.filter(owner=sub.owner).update(is_current=False)
            # Set this one current
            sub.is_current = True
            sub.save(update_fields=['is_current'])
            count += 1
        self.message_user(request, f"Marked {count} subscription(s) as current (unset others for each owner)")

    set_as_current.short_description = 'Mark as current (unset others for owner)'

    def set_cancel_at_period_end(self, request, queryset):
        updated = queryset.update(cancel_at_period_end=True)
        self.message_user(request, f"Set cancel at period end on {updated} subscription(s)")

    set_cancel_at_period_end.short_description = 'Set cancel at period end'

    def unset_cancel_at_period_end(self, request, queryset):
        updated = queryset.update(cancel_at_period_end=False)
        self.message_user(request, f"Unset cancel at period end on {updated} subscription(s)")

    unset_cancel_at_period_end.short_description = 'Unset cancel at period end'


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