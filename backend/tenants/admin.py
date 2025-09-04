from django.contrib import admin
from .models import Tenant, EmergencyContact, Stay, TenantBedHistory, BedHistory

# Inlines for Tenant detail page
class EmergencyContactInline(admin.TabularInline):
    model = EmergencyContact
    extra = 0
    fields = ("name", "relationship", "phone")


class StayInline(admin.TabularInline):
    model = Stay
    extra = 0
    fields = ("bed", "status", "check_in", "expected_check_out", "actual_check_out", "monthly_rent")
    autocomplete_fields = ("bed",)
    ordering = ("-check_in",)


class TenantBedHistoryInline(admin.TabularInline):
    model = TenantBedHistory
    extra = 0
    fields = ("bed", "started_on", "ended_on", "notes", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("bed",)
    ordering = ("-started_on", "-created_at")


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("full_name", "phone", "email", "building", "current_bed", "is_active")
    search_fields = ("full_name", "phone", "email", "id_proof_number", "building__name")
    list_filter = ("is_active", "gender", "building")
    readonly_fields = ("created_at", "created_by", "updated_at", "updated_by")
    inlines = [EmergencyContactInline, StayInline, TenantBedHistoryInline]
    list_select_related = ("building",)

    def current_bed(self, obj):
        # Prefer active > reserved > latest
        qs = obj.stays.select_related("bed").order_by("-created_at")
        active = qs.filter(status="active").first()
        if active:
            return active.bed
        reserved = qs.filter(status="reserved").first()
        if reserved:
            return reserved.bed
        any_stay = qs.first()
        return any_stay.bed if any_stay else None
    current_bed.short_description = "Current bed"


@admin.register(EmergencyContact)
class EmergencyContactAdmin(admin.ModelAdmin):
    list_display = ("tenant", "name", "phone", "relationship")
    search_fields = ("tenant__full_name", "name", "phone")
    autocomplete_fields = ("tenant",)


@admin.register(Stay)
class StayAdmin(admin.ModelAdmin):
    list_display = ("tenant", "bed", "status", "check_in", "monthly_rent", "maintenance_amount")
    list_filter = ("status",)
    search_fields = ("tenant__full_name", "bed__number")
    autocomplete_fields = ("tenant", "bed")
    list_select_related = ("tenant", "bed")


@admin.register(TenantBedHistory)
class BedHistoryAdmin(admin.ModelAdmin):
    list_display = ("tenant", "bed", "started_on", "ended_on", "created_at")
    list_filter = ("ended_on", "bed__room__floor__building",)
    search_fields = ("tenant__full_name", "bed__number", "notes")
    date_hierarchy = "started_on"
    ordering = ("-started_on", "-created_at")
    autocomplete_fields = ("tenant", "bed")

# Also register the proxy model for a bed-centric view in admin
admin.site.register(BedHistory, BedHistoryAdmin)
