from rest_framework import serializers
from .models import Invoice, Payment, Expense, InvoiceExpense, ExpenseCategory, InvoiceSettings
from bookings.models import Booking
from django.conf import settings
from bookings.serializers import BookingSerializer, TenantMiniSerializer
from django.db.models import Sum


class InvoiceSerializer(serializers.ModelSerializer):
    status = serializers.CharField(read_only=True)
    total_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    balance_due = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    # Write-only booking id for create/update
    booking_id = serializers.PrimaryKeyRelatedField(source="booking", queryset=Booking.objects.all(), write_only=True)
    # Read-only nested booking summary
    booking = BookingSerializer(read_only=True)
    # Read-only tenant mini derived from booking.tenant
    tenant = TenantMiniSerializer(source="booking.tenant", read_only=True)
    # Read-only: computed total of payments applied to this invoice
    payments_total = serializers.SerializerMethodField(read_only=True)
    # Read-only: nested invoice line items (expenses) for display
    expenses = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Invoice
        fields = [
            'id', 'booking_id', 'booking', 'tenant', 'cycle_month', 'issue_date', 'due_date',
            'amount', 'tax_amount', 'discount_amount', 'total_amount', 'balance_due',
            'status', 'notes', 'metadata', 'payments_total', 'expenses', 'created_at', 'updated_at'
        ]
        read_only_fields = ('id', 'total_amount', 'balance_due', 'status', 'payments_total', 'expenses', 'created_at', 'updated_at')

    def validate(self, attrs):
        # Model.clean() performs domain validation
        return super().validate(attrs)

    def get_payments_total(self, obj):
        try:
            agg = obj.payments.aggregate(s=Sum('amount'))
            return agg.get('s') or 0
        except Exception:
            return 0

    def get_expenses(self, obj):
        # Inline lightweight serializer to avoid reordering class definitions
        class _MiniInvoiceExpenseSerializer(serializers.ModelSerializer):
            class Meta:
                model = InvoiceExpense
                fields = ['id', 'label', 'amount', 'taxable', 'tax_rate', 'notes', 'created_at', 'updated_at']
        try:
            qs = getattr(obj, 'expenses', None)
            if qs is None:
                return []
            return _MiniInvoiceExpenseSerializer(qs.all(), many=True).data
        except Exception:
            return []


class MinimalUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField(allow_blank=True, required=False)
    name = serializers.CharField(allow_blank=True, required=False)
    email = serializers.EmailField(allow_blank=True, required=False)

    def to_representation(self, instance):
        if instance is None:
            return None
        # Handle both user model instance and primitive id
        try:
            first = getattr(instance, 'first_name', '') or ''
            last = getattr(instance, 'last_name', '') or ''
            full_name = f"{first} {last}".strip()
            return {
                'id': getattr(instance, 'id', instance),
                'username': getattr(instance, 'username', '') or '',
                'name': full_name or getattr(instance, 'name', '') or '',
                'email': getattr(instance, 'email', '') or '',
            }
        except Exception:
            return {'id': instance, 'username': '', 'name': '', 'email': ''}


class PaymentSerializer(serializers.ModelSerializer):
    # Make invoice optional/null allowed
    invoice = serializers.PrimaryKeyRelatedField(queryset=Invoice.objects.all(), allow_null=True, required=False)
    # Make method optional, choices validated dynamically in validate()
    method = serializers.CharField(allow_blank=True, required=False, allow_null=True)
    # Read-only user stamps
    created_by = MinimalUserSerializer(read_only=True)
    updated_by = MinimalUserSerializer(read_only=True)
    # Computed status to align with bookings.Payment API and frontend expectations
    status = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Payment
        fields = ['id', 'invoice', 'amount', 'method', 'reference', 'received_at', 'notes', 'status', 'created_at', 'updated_at', 'created_by', 'updated_by']
        read_only_fields = ('id', 'created_at', 'updated_at', 'created_by', 'updated_by')

    def _allowed_methods(self):
        # From settings e.g. ["cash","upi",...] else fallback to model enum values
        choices = getattr(settings, 'PAYMENT_METHOD_CHOICES', None)
        if isinstance(choices, (list, tuple)) and choices:
            # Normalize to lowercase strings
            return {str(c).lower() for c in choices}
        try:
            return set(Payment.Method.values)
        except Exception:
            return set()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        method = attrs.get('method', None)
        allowed = self._allowed_methods()
        # If method provided and we have an allowed list, enforce it
        if method not in (None, '') and allowed:
            if str(method).lower() not in allowed:
                raise serializers.ValidationError({
                    'method': f"Invalid method. Allowed: {', '.join(sorted(allowed))}"
                })
        return attrs

    def get_status(self, obj):
        """Derive a simple payment status for payments-app records.

        Rules:
        - If linked invoice exists and is 'paid' => 'success'.
        - If invoice is 'partial' or 'open' => 'pending'.
        - Otherwise default to 'success' (since a saved payment generally indicates a completed transaction in this app).
        """
        try:
            inv_status = getattr(getattr(obj, 'invoice', None), 'status', None)
            if inv_status == 'paid':
                return 'success'
            if inv_status in {'partial', 'open'}:
                return 'pending'
        except Exception:
            pass
        return 'success'


class ExpenseSerializer(serializers.ModelSerializer):
    created_by = MinimalUserSerializer(read_only=True)
    updated_by = MinimalUserSerializer(read_only=True)

    class Meta:
        model = Expense
        fields = [
            'id', 'amount', 'category', 'expense_date', 'description',
            'reference', 'building', 'attachment', 'metadata', 'created_at', 'updated_at',
            'created_by', 'updated_by'
        ]
        read_only_fields = ('id', 'created_at', 'updated_at', 'created_by', 'updated_by')


class ExpenseCategorySerializer(serializers.ModelSerializer):
    owner = MinimalUserSerializer(read_only=True)
    display_code = serializers.CharField(read_only=True)
    sequence = serializers.IntegerField(read_only=True)

    class Meta:
        model = ExpenseCategory
        fields = ['id', 'name', 'is_active', 'owner', 'display_code', 'sequence', 'created_at', 'updated_at']
        read_only_fields = ('id', 'owner', 'created_at', 'updated_at')


class InvoiceExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceExpense
        fields = ['id', 'invoice', 'label', 'amount', 'taxable', 'tax_rate', 'notes', 'created_at', 'updated_at']
        read_only_fields = ('id', 'created_at', 'updated_at')


class InvoiceSettingsSerializer(serializers.ModelSerializer):
    owner = MinimalUserSerializer(read_only=True)

    class Meta:
        model = InvoiceSettings
        fields = [
            'id', 'owner', 'building',
            'generate_type', 'period', 'generate_on',
            'monthly_cycle', 'monthly_custom_day',
            'weekly_cycle', 'weekly_custom_weekday',
            'notes', 'created_at', 'updated_at'
        ]
        read_only_fields = ('id', 'owner', 'created_at', 'updated_at')

    def validate(self, attrs):
        attrs = super().validate(attrs)
        monthly_cycle = attrs.get('monthly_cycle', getattr(self.instance, 'monthly_cycle', None))
        monthly_custom_day = attrs.get('monthly_custom_day', getattr(self.instance, 'monthly_custom_day', None))
        weekly_cycle = attrs.get('weekly_cycle', getattr(self.instance, 'weekly_cycle', None))
        weekly_custom_weekday = attrs.get('weekly_custom_weekday', getattr(self.instance, 'weekly_custom_weekday', None))
        # mirror model-level expectations for clearer API errors
        if monthly_cycle == InvoiceSettings.MonthlyCycle.CUSTOM_DAY and not monthly_custom_day:
            raise serializers.ValidationError({'monthly_custom_day': 'This field is required when monthly_cycle=custom_day.'})
        if weekly_cycle == InvoiceSettings.WeeklyCycle.CUSTOM_DAY and weekly_custom_weekday is None:
            raise serializers.ValidationError({'weekly_custom_weekday': 'This field is required when weekly_cycle=custom_day.'})
        return attrs
