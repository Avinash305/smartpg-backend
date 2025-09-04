from django.contrib import admin
from .models import Building, Floor, Room, Bed
from tenants.models import TenantBedHistory


@admin.register(Building)
class BuildingAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "state", "owner", "manager", "is_active", "created_at", "updated_at", "notes")
    list_filter = ("city", "state", "is_active", "property_type")
    search_fields = ("name", "city", "state", "code", "notes", "owner__email", "manager__email")
    readonly_fields = ("created_at", "created_by", "updated_at", "updated_by")
    autocomplete_fields = ("owner", "manager")
    date_hierarchy = "created_at"
    list_select_related = ("owner", "manager")


@admin.register(Floor)
class FloorAdmin(admin.ModelAdmin):
    list_display = ("building", "number", "created_at", "updated_at", "notes")
    list_filter = ("building",)
    search_fields = ("building__name", "notes")
    readonly_fields = ("created_at", "created_by", "updated_at", "updated_by")
    autocomplete_fields = ("building",)
    list_select_related = ("building",)


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("number", "floor", "room_type", "capacity", "monthly_rent", "is_active", "created_at", "updated_at", "notes")
    list_filter = ("room_type", "is_active", "floor__building")
    search_fields = ("number", "floor__building__name", "notes")
    readonly_fields = ("created_at", "created_by", "updated_at", "updated_by")
    autocomplete_fields = ("floor",)
    list_select_related = ("floor", "floor__building")


class TenantBedHistoryInline(admin.TabularInline):
    model = TenantBedHistory
    extra = 0
    fields = ("tenant", "started_on", "ended_on", "notes", "created_at")
    readonly_fields = ("created_at",)
    ordering = ("-started_on", "-created_at")
    autocomplete_fields = ("tenant",)


@admin.register(Bed)
class BedAdmin(admin.ModelAdmin):
    list_display = ("number", "room", "get_building", "status", "current_tenant_name", "history_count", "monthly_rent", "created_at", "updated_at")
    list_filter = ("status", "room__floor__building", "room__floor")
    search_fields = ("number", "room__number", "room__floor__building__name")
    readonly_fields = ("created_at", "created_by", "updated_at", "updated_by")
    inlines = [TenantBedHistoryInline]
    autocomplete_fields = ("room",)
    list_select_related = ("room", "room__floor", "room__floor__building")

    def get_building(self, obj):
        try:
            return obj.room.floor.building
        except Exception:
            return None
    get_building.short_description = "Building"

    def current_tenant_name(self, obj):
        tenant = obj.current_tenant
        return getattr(tenant, "full_name", None) if tenant else None
    current_tenant_name.short_description = "Current tenant"

    def history_count(self, obj):
        try:
            return obj.usage_history.count()
        except Exception:
            return 0
    history_count.short_description = "History entries"
