from rest_framework import serializers
from .models import Building, Floor, Room, Bed
from tenants.models import TenantBedHistory


class BuildingSerializer(serializers.ModelSerializer):
    created_by = serializers.StringRelatedField(read_only=True)
    updated_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = Building
        fields = [
            'id', 'owner', 'manager', 'name', 'code', 'property_type',
            'address_line', 'city', 'state', 'pincode', 'is_active', 'notes',
            'created_at', 'created_by', 'updated_at', 'updated_by',
        ]
        read_only_fields = ['created_at', 'created_by', 'updated_at', 'updated_by']


class FloorSerializer(serializers.ModelSerializer):
    building_name = serializers.CharField(source='building.name', read_only=True)
    created_by = serializers.StringRelatedField(read_only=True)
    updated_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = Floor
        fields = [
            'id', 'building', 'building_name', 'number', 'notes', 'is_active',
            'created_at', 'created_by', 'updated_at', 'updated_by',
        ]
        read_only_fields = ['created_at', 'created_by', 'updated_at', 'updated_by']


class RoomSerializer(serializers.ModelSerializer):
    floor_display = serializers.CharField(source='floor.get_number_display', read_only=True)
    capacity = serializers.IntegerField(read_only=True)
    created_by = serializers.StringRelatedField(read_only=True)
    updated_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = Room
        fields = [
            'id', 'floor', 'floor_display', 'number', 'room_type', 'capacity',
            'monthly_rent', 'security_deposit', 'is_active', 'notes',
            'created_at', 'created_by', 'updated_at', 'updated_by',
        ]
        read_only_fields = ['capacity', 'created_at', 'created_by', 'updated_at', 'updated_by']


class BedUsageHistorySerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source='tenant.full_name', read_only=True)

    class Meta:
        model = TenantBedHistory
        fields = [
            'id', 'tenant', 'tenant_name', 'bed', 'started_on', 'ended_on', 'notes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class BedSerializer(serializers.ModelSerializer):
    room_number = serializers.CharField(source='room.number', read_only=True)
    # Added for friendly activity names
    floor_display = serializers.CharField(source='room.floor.get_number_display', read_only=True)
    building_name = serializers.CharField(source='room.floor.building.name', read_only=True)

    created_by = serializers.StringRelatedField(read_only=True)
    updated_by = serializers.StringRelatedField(read_only=True)
    current_tenant_id = serializers.SerializerMethodField(read_only=True)
    current_tenant_name = serializers.SerializerMethodField(read_only=True)
    history_count = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Bed
        fields = [
            'id', 'room', 'room_number', 'number', 'status', 'monthly_rent', 'notes',
            'floor_display', 'building_name',
            'current_tenant_id', 'current_tenant_name', 'history_count',
            'created_at', 'created_by', 'updated_at', 'updated_by',
        ]
        read_only_fields = ['created_at', 'created_by', 'updated_at', 'updated_by']

    def get_current_tenant_id(self, obj):
        tenant = getattr(obj, 'current_tenant', None)
        return getattr(tenant, 'id', None)

    def get_current_tenant_name(self, obj):
        tenant = getattr(obj, 'current_tenant', None)
        return getattr(tenant, 'full_name', None) if tenant else None

    def get_history_count(self, obj):
        try:
            return obj.usage_history.count()
        except Exception:
            return 0
