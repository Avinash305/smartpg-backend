from rest_framework import serializers
from .models import Tenant, EmergencyContact, Stay, TenantBedHistory
from django.db import models
from django.db.models import Sum
from decimal import Decimal
from django.utils import timezone


class TenantSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    updated_by = serializers.StringRelatedField(read_only=True)
    building_id = serializers.SerializerMethodField(read_only=True)
    building_name = serializers.CharField(source='building.name', read_only=True)
    bed_history = serializers.SerializerMethodField(read_only=True)
    balance = serializers.SerializerMethodField(read_only=True)
    check_in = serializers.SerializerMethodField(read_only=True)
    check_out = serializers.SerializerMethodField(read_only=True)
    booking_status = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Tenant
        fields = [
            'id', 'full_name', 'email', 'phone', 'gender', 'date_of_birth',
            'address_line', 'city', 'state', 'pincode',
            'building', 'building_name',
            'id_proof_type', 'id_proof_number', 'id_proof_document', 'photo',
            'is_active', 'building_id', 'created_at', 'created_by', 'updated_at', 'updated_by',
            'bed_history', 'balance', 'check_in', 'check_out', 'booking_status',
        ]
        read_only_fields = ['created_at', 'created_by', 'updated_at', 'updated_by']

    def get_building_id(self, obj):
        # active > reserved > latest
        def _first(qs):
            return qs.select_related('bed__room__floor__building').order_by('-created_at').first()

        stay = _first(obj.stays.filter(status='active'))
        if not stay:
            stay = _first(obj.stays.filter(status='reserved'))
        if not stay:
            stay = _first(obj.stays.all())

        if stay and getattr(stay, 'bed', None):
            try:
                return stay.bed.room.floor.building_id
            except Exception:
                return None
        return None

    def get_bed_history(self, obj):
        qs = obj.bed_history.select_related('bed__room__floor__building').order_by('-started_on', '-created_at')
        return BedHistorySerializer(qs, many=True).data

    def get_balance(self, obj):
        """Calculate total outstanding balance from all invoices for this tenant."""
        try:
            # Import here to avoid circular imports
            from payment.models import Invoice
            from bookings.models import Booking
            
            # Get all bookings for this tenant
            bookings = Booking.objects.filter(tenant=obj)
            
            # Get all invoices for these bookings and sum the balance_due
            total_balance = Invoice.objects.filter(
                booking__in=bookings
            ).aggregate(
                total=Sum('balance_due')
            )['total']
            
            return float(total_balance or Decimal('0.00'))
        except Exception:
            # Return 0 if there's any error calculating balance
            return 0.00

    def _get_relevant_stay(self, obj):
        """Get the most relevant stay: active > reserved > latest."""
        # Try active first
        stay = obj.stays.filter(status='active').order_by('-created_at').first()
        if not stay:
            # Try reserved
            stay = obj.stays.filter(status='reserved').order_by('-created_at').first()
        if not stay:
            # Get latest stay
            stay = obj.stays.order_by('-created_at').first()
        return stay

    def get_check_in(self, obj):
        """Get check-in date from the most relevant stay."""
        stay = self._get_relevant_stay(obj)
        return stay.check_in if stay else None

    def get_check_out(self, obj):
        """Get check-out date from the most relevant stay."""
        stay = self._get_relevant_stay(obj)
        if stay:
            # Return actual_check_out if available, otherwise expected_check_out
            return stay.actual_check_out or stay.expected_check_out
        return None

    def get_booking_status(self, obj):
        """Return the most relevant booking status for this tenant.

        Preference order:
        - A live booking (pending/reserved/confirmed) overlapping today
        - Otherwise the latest live booking
        - Otherwise the latest booking of any status
        """
        try:
            from bookings.models import Booking  # local import to avoid circulars
            today = timezone.localdate()
            live_statuses = ["pending", "reserved", "confirmed"]
            # Prefer live overlapping today
            overlap_q = models.Q(start_date__lte=today) & (models.Q(end_date__isnull=True) | models.Q(end_date__gte=today))
            q = obj.bookings.filter(status__in=live_statuses).filter(overlap_q).order_by('-created_at')
            booking = q.first()
            if not booking:
                # Fallback: any live booking
                booking = obj.bookings.filter(status__in=live_statuses).order_by('-created_at').first()
            if not booking:
                # Fallback: any latest booking
                booking = obj.bookings.order_by('-created_at').first()
            return booking.status if booking else None
        except Exception:
            return None


class EmergencyContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmergencyContact
        fields = ['id', 'tenant', 'name', 'relationship', 'phone', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class StaySerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source='tenant.full_name', read_only=True)
    bed_number = serializers.CharField(source='bed.number', read_only=True)
    building_id = serializers.IntegerField(source='bed.room.floor.building_id', read_only=True)
    building_name = serializers.CharField(source='bed.room.floor.building.name', read_only=True)
    created_by = serializers.StringRelatedField(read_only=True)
    updated_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = Stay
        fields = [
            'id', 'tenant', 'tenant_name', 'bed', 'bed_number',
            'building_id', 'building_name', 'status',
            'check_in', 'expected_check_out', 'actual_check_out',
            'monthly_rent', 'security_deposit', 'maintenance_amount',
            'notes', 'created_at', 'created_by', 'updated_at', 'updated_by',
        ]
        read_only_fields = ['created_at', 'created_by', 'updated_at', 'updated_by']


class BedHistorySerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source='tenant.full_name', read_only=True)
    bed_number = serializers.CharField(source='bed.number', read_only=True)
    room_number = serializers.CharField(source='bed.room.number', read_only=True)
    building_id = serializers.IntegerField(source='bed.room.floor.building_id', read_only=True)
    building_name = serializers.CharField(source='bed.room.floor.building.name', read_only=True)
    stay_status = serializers.SerializerMethodField(read_only=True)
    booking_status = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = TenantBedHistory
        fields = [
            'id', 'tenant', 'tenant_name', 'bed', 'bed_number', 'room_number',
            'building_id', 'building_name', 'started_on', 'ended_on', 'notes',
            'stay_status', 'booking_status', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_stay_status(self, obj):
        """
        Approximate the stay status for this history row by finding a Stay for the same
        tenant/bed that overlaps the history start date.
        Returns one of: 'reserved', 'active', 'completed', or None.
        """
        try:
            stay = (
                Stay.objects
                .filter(tenant=obj.tenant, bed=obj.bed)
                .filter(
                    # started_on should fall on/after check_in
                    check_in__lte=obj.started_on
                )
                .order_by('-created_at')
                .first()
            )
            if not stay:
                # fallback: latest stay for same tenant/bed
                stay = (
                    Stay.objects
                    .filter(tenant=obj.tenant, bed=obj.bed)
                    .order_by('-created_at')
                    .first()
                )
            return stay.status if stay else None
        except Exception:
            return None

    def get_booking_status(self, obj):
        """
        Find the most relevant Booking for the same tenant/bed whose window overlaps
        the history started_on date, and return its status.
        Returns one of: 'pending', 'reserved', 'confirmed', 'canceled', 'converted', or None.
        """
        try:
            from bookings.models import Booking  # local import to avoid circulars
            q = Booking.objects.filter(tenant=obj.tenant, bed=obj.bed)
            # Overlap if booking started on/before history start and either open-ended or ends after start
            q = q.filter(start_date__lte=obj.started_on).filter(models.Q(end_date__isnull=True) | models.Q(end_date__gte=obj.started_on))
            booking = q.order_by('-created_at').first()
            if not booking:
                # fallback to latest booking for same tenant/bed
                booking = (
                    Booking.objects
                    .filter(tenant=obj.tenant, bed=obj.bed)
                    .order_by('-created_at')
                    .first()
                )
            return booking.status if booking else None
        except Exception:
            return None
