from django.contrib import admin
from .models import Booking, Payment, BookingMovement, BookingMedia


# Register your models here.

class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("amount", "method", "status", "paid_on", "reference")
    readonly_fields = ()
    ordering = ("-paid_on",)


class BookingMediaInline(admin.TabularInline):
    model = BookingMedia
    extra = 0
    fields = ("file", "file_size", "content_type", "created_at")
    readonly_fields = ("file_size", "content_type", "created_at")
    ordering = ("-created_at",)


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
    inlines = [BookingMediaInline, PaymentInline]


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


@admin.register(BookingMedia)
class BookingMediaAdmin(admin.ModelAdmin):
    list_display = ("id", "booking", "owner", "file_name", "file_size", "content_type", "created_at")
    list_filter = ("content_type", "created_at")
    search_fields = (
        "booking__id",
        "booking__tenant__full_name",
        "owner__username",
        "owner__email",
    )
    readonly_fields = ("file_size", "content_type", "created_at")
    autocomplete_fields = ("booking", "owner")
    list_select_related = ("booking", "owner")
    ordering = ("-created_at",)

    def file_name(self, obj):
        try:
            return getattr(getattr(obj.file, 'name', None), 'split', lambda x: None)('/')[-1] if obj.file else ''
        except Exception:
            return str(obj.file) if obj.file else ''
    file_name.short_description = 'File'
