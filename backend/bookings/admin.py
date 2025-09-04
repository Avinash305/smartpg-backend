from django.contrib import admin
from .models import Booking, Payment, BookingMovement


# Register your models here.

class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("amount", "method", "status", "paid_on", "reference")
    readonly_fields = ()
    ordering = ("-paid_on",)


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tenant",
        "status",
        "building",
        "floor",
        "room",
        "bed",
        "monthly_rent",
        "security_deposit",
        "maintenance_amount",
        "booked_at",
        "booked_by",
        "created_at",
        "updated_at",
    )
    list_filter = (
        "status",
        "building",
        "floor",
        "room",
        "bed",
        "booked_by",
    )
    search_fields = (
        "tenant__full_name",
        "tenant__phone",
        "building__name",
        "room__number",
        "bed__number",
    )
    date_hierarchy = "booked_at"
    autocomplete_fields = ("tenant", "building", "floor", "room", "bed", "booked_by")
    list_select_related = ("tenant", "building", "floor", "room", "bed", "booked_by")
    inlines = [PaymentInline]


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "booking", "amount", "method", "status", "paid_on")
    list_filter = ("method", "status")
    search_fields = ("booking__tenant__full_name", "booking__tenant__phone", "reference")
    date_hierarchy = "paid_on"
    autocomplete_fields = ("booking",)
    list_select_related = ("booking",)


@admin.register(BookingMovement)
class BookingMovementAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "booking",
        "moved_at",
        "old_tenant",
        "new_tenant",
        "moved_by",
        "from_building",
        "from_floor",
        "from_room",
        "from_bed",
        "to_building",
        "to_floor",
        "to_room",
        "to_bed",
    )
    list_filter = (
        "from_building",
        "from_floor",
        "from_room",
        "from_bed",
        "to_building",
        "to_floor",
        "to_room",
        "to_bed",
        "moved_by",
    )
    search_fields = (
        "booking__id",
        "old_tenant__full_name",
        "old_tenant__phone",
        "new_tenant__full_name",
        "new_tenant__phone",
        "moved_by__username",
        "moved_by__email",
    )
    date_hierarchy = "moved_at"
    autocomplete_fields = (
        "booking",
        "old_tenant",
        "new_tenant",
        "from_building",
        "from_floor",
        "from_room",
        "from_bed",
        "to_building",
        "to_floor",
        "to_room",
        "to_bed",
        "moved_by",
    )
