from rest_framework import serializers
from .models import Booking, Payment, BookingMovement

class TenantMiniSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    full_name = serializers.CharField(allow_blank=True, required=False)
    email = serializers.EmailField(allow_blank=True, required=False)
    phone = serializers.CharField(allow_blank=True, required=False)

    def to_representation(self, instance):
        if instance is None:
            return None
        return {
            "id": getattr(instance, "id", None),
            "full_name": getattr(instance, "full_name", ""),
            "email": getattr(instance, "email", ""),
            "phone": getattr(instance, "phone", ""),
        }

class BookingMovementSerializer(serializers.ModelSerializer):
    old_tenant = TenantMiniSerializer(read_only=True)
    new_tenant = TenantMiniSerializer(read_only=True)

    from_location = serializers.SerializerMethodField()
    to_location = serializers.SerializerMethodField()
    moved_by = serializers.SerializerMethodField()

    class Meta:
        model = BookingMovement
        fields = [
            "id",
            "moved_at",
            "old_tenant",
            "new_tenant",
            "from_location",
            "to_location",
            "notes",
            "moved_by",
        ]

    def get_from_location(self, obj):
        return {
            "building": getattr(obj.from_building, "name", None),
            "floor": getattr(obj.from_floor, "get_number_display", lambda: None)(),
            "room": getattr(obj.from_room, "number", None),
            "bed": getattr(obj.from_bed, "number", None),
            "building_name": getattr(obj.from_building, "name", None),
            "floor_display": getattr(obj.from_floor, "get_number_display", lambda: None)(),
            "room_number": getattr(obj.from_room, "number", None),
            "bed_number": getattr(obj.from_bed, "number", None),
        }

    def get_to_location(self, obj):
        return {
            "building": getattr(obj.to_building, "name", None),
            "floor": getattr(obj.to_floor, "get_number_display", lambda: None)(),
            "room": getattr(obj.to_room, "number", None),
            "bed": getattr(obj.to_bed, "number", None),
            "building_name": getattr(obj.to_building, "name", None),
            "floor_display": getattr(obj.to_floor, "get_number_display", lambda: None)(),
            "room_number": getattr(obj.to_room, "number", None),
            "bed_number": getattr(obj.to_bed, "number", None),
        }

    def get_moved_by(self, obj):
        user = getattr(obj, "moved_by", None)
        if not user:
            return None
        return {
            "id": getattr(user, "id", None),
            "username": getattr(user, "username", ""),
            "email": getattr(user, "email", ""),
        }

class BookingSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source="tenant.full_name", read_only=True)
    building_name = serializers.CharField(source="building.name", read_only=True)
    building_address_line = serializers.CharField(source="building.address_line", read_only=True)
    building_city = serializers.CharField(source="building.city", read_only=True)
    building_state = serializers.CharField(source="building.state", read_only=True)
    building_pincode = serializers.CharField(source="building.pincode", read_only=True)
    building_address = serializers.SerializerMethodField(read_only=True)
    floor_display = serializers.CharField(source="floor.get_number_display", read_only=True)
    room_number = serializers.CharField(source="room.number", read_only=True)
    bed_number = serializers.CharField(source="bed.number", read_only=True)
    booked_by_email = serializers.EmailField(source="booked_by.email", read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)
    updated_by_email = serializers.EmailField(source="updated_by.email", read_only=True)
    movement_history = BookingMovementSerializer(source="movements", many=True, read_only=True)

    def get_building_address(self, obj):
        b = getattr(obj, "building", None)
        if not b:
            return ""
        parts = [
            getattr(b, "address_line", "") or "",
            getattr(b, "city", "") or "",
            getattr(b, "state", "") or "",
            getattr(b, "pincode", "") or "",
        ]
        return ", ".join([p for p in parts if p])

    class Meta:
        model = Booking
        fields = [
            "id",
            "tenant", "tenant_name",
            "building", "building_name",
            "building_address_line", "building_city", "building_state", "building_pincode", "building_address",
            "floor", "floor_display",
            "room", "room_number",
            "bed", "bed_number",
            "status", "source",
            "start_date", "end_date",
            "monthly_rent", "security_deposit",
            "discount_amount", "maintenance_amount",
            "notes",
            "booked_at", "booked_by", "booked_by_email",
            "created_at", "created_by", "created_by_email",
            "updated_at", "updated_by", "updated_by_email",
            "movement_history",
        ]
        read_only_fields = [
            "booked_at", "booked_by",
            "created_at", "created_by",
            "updated_at", "updated_by",
        ]

class PaymentSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source="booking.tenant.full_name", read_only=True)

    class Meta:
        model = Payment
        fields = [
            "id", "booking", "tenant_name",
            "amount", "method", "status",
            "paid_on", "reference", "notes",
            "created_at", "created_by", "updated_at", "updated_by",
        ]
        read_only_fields = ["created_at", "created_by", "updated_at", "updated_by"]