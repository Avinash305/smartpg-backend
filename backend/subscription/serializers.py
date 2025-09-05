from rest_framework import serializers
from django.utils import timezone
from .models import SubscriptionPlan, Subscription


class SubscriptionPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionPlan
        fields = [
            'id', 'name', 'slug', 'currency', 'price_monthly', 'price_yearly',
            'is_active', 'features', 'limits', 'available_intervals', 'prices', 'created_at', 'updated_at',
            # New discount fields
            'discount_active', 'discount_type', 'discount_value', 'discount_currency',
            'discount_valid_from', 'discount_valid_until', 'discount_allowed_intervals', 'discount_description',
        ]
        read_only_fields = ['created_at', 'updated_at']


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = SubscriptionPlanSerializer(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'owner', 'plan', 'status', 'billing_interval', 'current_period_start', 'current_period_end',
            'trial_end', 'cancel_at_period_end', 'is_current', 'meta', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'owner', 'status', 'is_current', 'created_at', 'updated_at'
        ]
