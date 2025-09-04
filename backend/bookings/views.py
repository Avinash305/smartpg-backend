from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.dateparse import parse_date
from django.utils import timezone
from subscription.utils import ensure_feature

from .models import Booking, Payment, BookingMovement
from .serializers import BookingSerializer, PaymentSerializer


class BookingViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = BookingSerializer
    queryset = (
        Booking.objects.all()
        .select_related(
            "tenant",
            "building",
            "floor",
            "room",
            "bed",
            "booked_by",
        )
        .prefetch_related(
            "movements",
            "movements__old_tenant",
            "movements__new_tenant",
            "movements__from_building",
            "movements__from_floor",
            "movements__from_room",
            "movements__from_bed",
            "movements__to_building",
            "movements__to_floor",
            "movements__to_room",
            "movements__to_bed",
            "movements__moved_by",
        )
        .filter(building__is_active=True)
        .order_by("-booked_at", "-created_at")
    )

    def get_queryset(self):
        qs = super().get_queryset()
        # Ownership isolation: superuser all, pg_admin only own, pg_staff only their admin's
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not user.is_superuser:
            role = getattr(user, "role", None)
            if role == "pg_admin":
                qs = qs.filter(building__owner=user)
            elif role == "pg_staff" and getattr(user, "pg_admin_id", None):
                qs = qs.filter(building__owner_id=user.pg_admin_id)
            else:
                return qs.none()
        params = self.request.query_params
        # Filters
        tenant = params.get("tenant")
        building = params.get("building")
        floor = params.get("floor")
        room = params.get("room")
        bed = params.get("bed")
        status_ = params.get("status")
        start_from = parse_date(params.get("start_from")) if params.get("start_from") else None
        start_to = parse_date(params.get("start_to")) if params.get("start_to") else None

        if tenant:
            qs = qs.filter(tenant_id=tenant)
        if building:
            qs = qs.filter(building_id=building)
        if floor:
            qs = qs.filter(floor_id=floor)
        if room:
            qs = qs.filter(room_id=room)
        if bed:
            qs = qs.filter(bed_id=bed)
        if status_:
            qs = qs.filter(status=status_)
        if start_from:
            qs = qs.filter(start_date__gte=start_from)
        if start_to:
            qs = qs.filter(start_date__lte=start_to)
        return qs

    def perform_create(self, serializer):
        # Block creation in inactive buildings
        bld = serializer.validated_data.get('building')
        if bld and not getattr(bld, 'is_active', True):
            raise DRFValidationError({'building': 'Building is inactive. Activate the building to create bookings.'})
        # If bed is provided, ensure its building is active too
        bed = serializer.validated_data.get('bed')
        if bed:
            building = getattr(getattr(getattr(bed, 'room', None), 'floor', None), 'building', None)
            if building and not getattr(building, 'is_active', True):
                raise DRFValidationError({'bed': 'Selected bed belongs to an inactive building. Activate the building to create bookings.'})
        return super().perform_create(serializer)

    def perform_update(self, serializer):
        # Capture old values before save
        inst = serializer.instance
        old_tenant_id = inst.tenant_id
        old_building_id = inst.building_id
        old_floor_id = inst.floor_id
        old_room_id = inst.room_id
        old_bed_id = inst.bed_id

        # Block updates that move booking into an inactive building
        new_building = serializer.validated_data.get('building', inst.building)
        if new_building and not getattr(new_building, 'is_active', True):
            raise DRFValidationError({'building': 'Building is inactive. Activate the building to modify bookings.'})
        new_bed = serializer.validated_data.get('bed', inst.bed)
        if new_bed:
            building = getattr(getattr(getattr(new_bed, 'room', None), 'floor', None), 'building', None)
            if building and not getattr(building, 'is_active', True):
                raise DRFValidationError({'bed': 'Selected bed belongs to an inactive building. Activate the building to modify bookings.'})

        booking = serializer.save()

        changed = (
            (old_tenant_id != booking.tenant_id)
            or (old_building_id != booking.building_id)
            or (old_floor_id != booking.floor_id)
            or (old_room_id != booking.room_id)
            or (old_bed_id != booking.bed_id)
        )

        if changed:
            notes = self.request.data.get("move_notes") or self.request.data.get("notes") or ""
            user = getattr(self.request, "user", None)
            moved_by = user if (user and getattr(user, "is_authenticated", False)) else None
            BookingMovement.objects.create(
                booking=booking,
                moved_at=timezone.now(),
                old_tenant_id=old_tenant_id,
                new_tenant_id=booking.tenant_id,
                from_building_id=old_building_id,
                from_floor_id=old_floor_id,
                from_room_id=old_room_id,
                from_bed_id=old_bed_id,
                to_building_id=booking.building_id,
                to_floor_id=booking.floor_id,
                to_room_id=booking.room_id,
                to_bed_id=booking.bed_id,
                notes=notes,
                moved_by=moved_by,
            )

    def create(self, request, *args, **kwargs):
        # Require subscription feature for bookings
        ensure_feature(request.user, 'bookings')
        try:
            return super().create(request, *args, **kwargs)
        except DjangoValidationError as e:
            detail = e.message_dict if hasattr(e, "message_dict") else {"detail": e.messages if hasattr(e, "messages") else str(e)}
            raise DRFValidationError(detail)


class PaymentViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentSerializer
    queryset = (
        Payment.objects.all()
        .select_related("booking", "booking__tenant")
        .filter(booking__building__is_active=True)
        .order_by("-paid_on")
    )

    def get_queryset(self):
        qs = super().get_queryset()
        # Ownership isolation based on booking.building.owner
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not user.is_superuser:
            role = getattr(user, "role", None)
            if role == "pg_admin":
                qs = qs.filter(booking__building__owner=user)
            elif role == "pg_staff" and getattr(user, "pg_admin_id", None):
                qs = qs.filter(booking__building__owner_id=user.pg_admin_id)
            else:
                return qs.none()
        params = self.request.query_params
        booking = params.get("booking")
        tenant = params.get("tenant")
        status_ = params.get("status")
        method = params.get("method")
        paid_from = parse_date(params.get("paid_from")) if params.get("paid_from") else None
        paid_to = parse_date(params.get("paid_to")) if params.get("paid_to") else None
        # New: building filters
        building = params.get("building")
        building_in = params.get("building__in")

        if booking:
            qs = qs.filter(booking_id=booking)
        if tenant:
            qs = qs.filter(booking__tenant_id=tenant)
        if status_:
            qs = qs.filter(status=status_)
        if method:
            qs = qs.filter(method=method)
        if paid_from:
            qs = qs.filter(paid_on__date__gte=paid_from)
        if paid_to:
            qs = qs.filter(paid_on__date__lte=paid_to)
        # Apply building filters
        if building:
            qs = qs.filter(booking__building_id=building)
        if building_in:
            try:
                ids = [int(x) for x in str(building_in).split(',') if str(x).strip()]
                if ids:
                    qs = qs.filter(booking__building_id__in=ids)
            except Exception:
                pass
        return qs

    def perform_create(self, serializer):
        # Block creation when the booking's building is inactive
        booking = serializer.validated_data.get('booking')
        if booking:
            bld = getattr(booking, 'building', None)
            if bld and not getattr(bld, 'is_active', True):
                raise DRFValidationError({'booking': 'Cannot add payments for a booking in an inactive building.'})
        return super().perform_create(serializer)

    def create(self, request, *args, **kwargs):
        # Require subscription feature for payments
        ensure_feature(request.user, 'payments')
        try:
            return super().create(request, *args, **kwargs)
        except DjangoValidationError as e:
            detail = e.message_dict if hasattr(e, "message_dict") else {"detail": e.messages if hasattr(e, "messages") else str(e)}
            raise DRFValidationError(detail)
