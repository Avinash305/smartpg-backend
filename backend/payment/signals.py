from django.db.models.signals import post_migrate, post_save
from django.dispatch import receiver
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in
from django.utils import timezone
from decimal import Decimal
from django.db import transaction
from django.db import models
import logging

from bookings.models import Booking

logger = logging.getLogger(__name__)

DEFAULT_EXPENSE_CATEGORIES = [
    "Building Rent/Lease",
    "Electricity Bill",
    "Water Bill",
    "Internet/Wi-Fi",
    "Housekeeping & Cleaning",
    "Staff Salaries",
    "Maintenance & Repairs",
    "Furniture & Fixtures",
    "Kitchen & Groceries",
    "Gas Cylinder / Cooking Fuel",
    "Laundry Services",
    "Security Services",
    "License & Compliance Fees",
    "Marketing & Advertising",
    "Software & IT Services",
    "other",
]


@receiver(post_migrate)
def seed_expense_categories(sender, **kwargs):
    """No-op for global categories: defaults are now per pg_admin only (see post_save hook)."""
    app_label = getattr(sender, 'label', None)
    if app_label != 'payment':
        return
    return


@receiver(post_save, sender=get_user_model())
def seed_admin_expense_categories(sender, instance=None, created=False, **kwargs):
    """When a new pg_admin user is created, seed owner-specific default categories.

    Uses the per-owner unique constraint on (Lower(name), owner) to avoid duplicates.
    """
    if not created or instance is None:
        return
    # Only for users with role 'pg_admin'
    role = getattr(instance, 'role', None)
    if role != 'pg_admin':
        return
    try:
        from .models import ExpenseCategory
    except Exception:
        return
    for key in DEFAULT_EXPENSE_CATEGORIES:
        name = key.replace('_', ' ').title()
        ExpenseCategory.objects.get_or_create(name=name, owner_id=getattr(instance, 'id', None), defaults={"is_active": True})


def _ensure_default_categories_for_owner(owner_id: int):
    if not owner_id:
        return
    try:
        from .models import ExpenseCategory
    except Exception:
        return
    for key in DEFAULT_EXPENSE_CATEGORIES:
        name = key.replace('_', ' ').title()
        ExpenseCategory.objects.get_or_create(
            owner_id=owner_id,
            name=name,
            defaults={"is_active": True},
        )


@receiver(user_logged_in)
def ensure_defaults_on_login(sender, user, request, **kwargs):
    """Dynamically ensure defaults exist whenever relevant users log in.

    - pg_admin: ensure their own defaults.
    - pg_staff: ensure their admin’s defaults.
    """
    role = getattr(user, 'role', None)
    if role == 'pg_admin':
        _ensure_default_categories_for_owner(getattr(user, 'id', None))
    elif role == 'pg_staff':
        _ensure_default_categories_for_owner(getattr(user, 'pg_admin_id', None))


# --- Immediate first invoice on booking confirmation ---
@receiver(post_save, sender=Booking)
def create_first_invoice_on_confirm(sender, instance: Booking, created: bool, **kwargs):
    """Create the first invoice immediately when a booking is confirmed and has a check-in date.

    Logic:
    - Trigger when created or status changed, and current status == confirmed.
    - Require start_date (check-in).
    - Resolve InvoiceSettings (building override -> owner global; fallback to defaults).
    - Only proceed for generate_type=automatic and period=monthly.
    - Compute window anchored to the booking's check-in month using booking.start_date as reference.
    - Honor generate_on:
        • START: create immediately for the first cycle window.
        • END: only create if today == window['end'] (else nightly task will handle at period end).
    - Create invoice if (booking, cycle_month=start) doesn't exist yet.
    """
    try:
        # Only proceed if now confirmed
        if getattr(instance, 'status', None) != 'confirmed':
            logger.debug('payment.signals:create_first_invoice_on_confirm skip: status=%s', getattr(instance, 'status', None))
            return

        # Only on create or status transition to confirmed
        old_status = getattr(instance, '_old_status', None)
        if not created and not (old_status and old_status != instance.status):
            logger.debug('payment.signals:create_first_invoice_on_confirm skip: not created and no status transition (old=%s new=%s)', old_status, instance.status)
            return

        # Need check-in date
        if not getattr(instance, 'start_date', None):
            logger.warning('payment.signals:create_first_invoice_on_confirm skip: missing start_date for booking id=%s', getattr(instance, 'id', None))
            return

        # Resolve owner/building
        building = getattr(instance, 'building', None)
        owner = getattr(building, 'owner', None) if building else None
        if not owner:
            logger.warning('payment.signals:create_first_invoice_on_confirm skip: missing owner/building for booking id=%s', getattr(instance, 'id', None))
            return

        # Lazy imports to avoid circulars
        from .models import Invoice, InvoiceSettings, InvoiceExpense

        # Resolve settings: building-specific else owner-global else defaults
        settings_obj = (
            InvoiceSettings.objects.filter(owner=owner, building_id=getattr(instance, 'building_id', None)).first()
            or InvoiceSettings.objects.filter(owner=owner, building__isnull=True).first()
        )
        if not settings_obj:
            settings_obj = InvoiceSettings(
                owner=owner,
                building=None,
                generate_type=InvoiceSettings.GenerateType.AUTOMATIC,
                period=InvoiceSettings.Period.MONTHLY,
                generate_on=InvoiceSettings.GenerateOn.START,
                monthly_cycle=InvoiceSettings.MonthlyCycle.CHECKIN_DATE,
            )

        # Respect settings: only automatic + monthly produce first invoice here
        if (
            settings_obj.generate_type != InvoiceSettings.GenerateType.AUTOMATIC
            or settings_obj.period != InvoiceSettings.Period.MONTHLY
        ):
            logger.debug('payment.signals:create_first_invoice_on_confirm skip: settings not automatic+monthly (type=%s period=%s)', settings_obj.generate_type, settings_obj.period)
            return

        # Compute window anchored to the check-in month
        window = settings_obj.monthly_period_window(reference_date=instance.start_date, booking=instance)
        start = window.get('start')
        end = window.get('end')
        gen_on = start if settings_obj.generate_on == InvoiceSettings.GenerateOn.START else end
        if not start or not end:
            logger.error('payment.signals:create_first_invoice_on_confirm abort: failed to compute window for booking id=%s', getattr(instance, 'id', None))
            return

        # If settings specify END, only create on the end date; otherwise create immediately
        today = timezone.localdate()
        if settings_obj.generate_on == InvoiceSettings.GenerateOn.END and today != gen_on:
            logger.info('payment.signals:create_first_invoice_on_confirm defer: generate_on=end; will generate on %s via beat', gen_on)
            return

        # Idempotency: normalize to first day when checking duplicates (matches saved value)
        try:
            from .models import Invoice as _InvModel
            start_first = _InvModel._first_day_of_month(start)
        except Exception:
            start_first = start
        if Invoice.objects.filter(booking=instance, cycle_month=start_first).exists():
            logger.info('payment.signals:create_first_invoice_on_confirm noop: invoice exists for booking=%s cycle_month=%s', getattr(instance, 'id', None), start)
            return

        # First invoice includes rent + maintenance - discount + security deposit
        base = (
            (instance.monthly_rent or Decimal('0'))
            + (instance.maintenance_amount or Decimal('0'))
            - (instance.discount_amount or Decimal('0'))
            + (instance.security_deposit or Decimal('0'))
        )
        if base < 0:
            base = Decimal('0.00')

        with transaction.atomic():
            # Ensure cycle_month is the first day of the month to pass Invoice.clean month-bound checks
            try:
                from .models import Invoice as _InvModel
                cycle_month = _InvModel._first_day_of_month(start)
            except Exception:
                cycle_month = start
            inv = Invoice(
                booking=instance,
                cycle_month=cycle_month,
                issue_date=today,
                due_date=end,
                amount=base,
                tax_amount=Decimal('0.00'),
                discount_amount=Decimal('0.00'),
                notes=f"Auto-generated at confirmation for period {start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}",
                status=Invoice.Status.OPEN,
            )
            inv.save()
            # Add descriptive line items to the first invoice only
            try:
                sec = instance.security_deposit or Decimal('0')
                if sec > 0:
                    InvoiceExpense.objects.create(
                        invoice=inv,
                        label="Security Deposit",
                        amount=sec,
                        taxable=False,
                        notes="Refundable after check-out",
                    )
                maint = instance.maintenance_amount or Decimal('0')
                if maint > 0:
                    InvoiceExpense.objects.create(
                        invoice=inv,
                        label="Maintenance (INR)",
                        amount=maint,
                        taxable=False,
                    )
                disc = instance.discount_amount or Decimal('0')
                if disc > 0:
                    InvoiceExpense.objects.create(
                        invoice=inv,
                        label="Discount (INR)",
                        amount=disc,
                        taxable=False,
                        notes="Applied as discount in totals",
                    )
            except Exception:
                # Line items are optional; do not block invoice creation
                pass
            logger.info('payment.signals:create_first_invoice_on_confirm created invoice id=%s for booking=%s', getattr(inv, 'id', None), getattr(instance, 'id', None))
    except Exception as e:
        # Never block booking save flow
        logger.exception('payment.signals:create_first_invoice_on_confirm error for booking id=%s: %s', getattr(instance, 'id', None), e)
        return


# --- Auto-void future invoices when a booking is cancelled ---
@receiver(post_save, sender=Booking)
def void_invoices_on_booking_cancel(sender, instance: Booking, created: bool, **kwargs):
    """When a booking transitions to 'canceled', void draft/open invoices from the current month onward.

    Rules:
    - Trigger only on status change to 'canceled'.
    - Find invoices for this booking with status in {draft, open} and cycle_month >= first day of today's month.
    - Mark them as VOID and append a note.
    - Do not alter PARTIAL/PAID invoices; handle manually if needed.
    """
    try:
        # Only on transition to canceled
        if getattr(instance, 'status', None) != 'canceled':
            return
        old_status = getattr(instance, '_old_status', None)
        if created or not (old_status and old_status != instance.status):
            return

        from .models import Invoice
        today = timezone.localdate()
        first_of_month_today = Invoice._first_day_of_month(today)

        qs = (
            Invoice.objects.filter(booking=instance)
            .filter(status__in=[Invoice.Status.DRAFT, Invoice.Status.OPEN])
            .filter(cycle_month__gte=first_of_month_today)
        )
        count = qs.update(status=Invoice.Status.VOID, notes=models.F('notes') + f"\nAuto-voided due to booking cancellation on {today.isoformat()}.")
        logger.info('payment.signals:void_invoices_on_booking_cancel: voided %s invoices for booking=%s', count, getattr(instance, 'id', None))
    except Exception as e:
        logger.exception('payment.signals:void_invoices_on_booking_cancel error for booking id=%s: %s', getattr(instance, 'id', None), e)
        return
