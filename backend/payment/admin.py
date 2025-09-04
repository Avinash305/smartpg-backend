from django.contrib import admin
from .models import Invoice, Payment, Expense, InvoiceExpense, ExpenseCategory, InvoiceSettings


# Register your models here.

@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("id", "booking", "cycle_month", "issue_date", "due_date", "total_amount", "balance_due", "status")
    list_filter = ("status", "cycle_month")
    search_fields = ("id", "booking__tenant__full_name", "booking__tenant__phone")
    date_hierarchy = "issue_date"
    autocomplete_fields = ("booking",)
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("booking", "booking__tenant")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("id", "invoice", "amount", "method", "received_at", "has_invoice")
    list_filter = (
        "method",
        ("invoice", admin.EmptyFieldListFilter),
        "received_at",
    )
    search_fields = ("id", "invoice__id", "reference")
    date_hierarchy = "received_at"
    autocomplete_fields = ("invoice",)
    readonly_fields = ("created_at", "updated_at", "created_by", "updated_by")
    list_select_related = ("invoice", "invoice__booking")

    @admin.display(boolean=True, description="Has Invoice")
    def has_invoice(self, obj: Payment):
        return bool(getattr(obj, "invoice_id", None))


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("id", "category", "amount", "expense_date", "building")
    list_filter = ("category", "building")
    search_fields = ("id", "description", "reference", "building__name")
    date_hierarchy = "expense_date"
    autocomplete_fields = ("building",)
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("building",)


@admin.register(InvoiceExpense)
class InvoiceExpenseAdmin(admin.ModelAdmin):
    list_display = ("id", "invoice", "label", "amount", "taxable", "tax_rate")
    list_filter = ("taxable",)
    search_fields = ("id", "label", "invoice__id")
    autocomplete_fields = ("invoice",)
    readonly_fields = ("created_at", "updated_at")
    list_select_related = ("invoice",)


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "is_active", "created_at", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("name",)
    actions = ["activate_categories", "deactivate_categories"]
    readonly_fields = ("created_at", "updated_at")

    @admin.action(description="Activate selected categories")
    def activate_categories(self, request, queryset):
        queryset.update(is_active=True)

    @admin.action(description="Deactivate selected categories")
    def deactivate_categories(self, request, queryset):
        queryset.update(is_active=False)


@admin.register(InvoiceSettings)
class InvoiceSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id", "owner", "building", "generate_type", "period", "generate_on",
        "monthly_cycle", "monthly_custom_day", "weekly_cycle", "weekly_custom_weekday", "created_at"
    )
    list_filter = (
        "generate_type", "period", "generate_on", "monthly_cycle", "weekly_cycle", "building"
    )
    search_fields = ("owner__email", "owner__full_name", "building__name")
    autocomplete_fields = ("owner", "building")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("owner", "building")
