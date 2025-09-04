from __future__ import annotations
from datetime import date as _date, timedelta
from decimal import Decimal

from celery import shared_task
from django.db import transaction
from django.utils import timezone
from django.db.models import Q

from bookings.models import Booking
from .models import Invoice, InvoiceSettings


def _clamp(year: int, month: int, day: int) -> _date:
    from calendar import monthrange
    last = monthrange(year, month)[1]
    return _date(year, month, max(1, min(day, last)))


def _month_add_same_day(d: _date, months: int = 1) -> _date:
    y = d.year
    m = d.month + months
    y += (m - 1) // 12
    m = ((m - 1) % 12) + 1
    return _clamp(y, m, d.day)


@shared_task(name="payment.tasks.generate_monthly_invoices")
def generate_monthly_invoices() -> int:
    """Generate invoices according to InvoiceSettings (automatic + monthly only).

    For each active confirmed booking today:
    - Resolve effective settings: building-specific override else owner-global; if none, use defaults from model.
    - Only proceed if generate_type=automatic and period=monthly.
    - Compute period window via settings.monthly_period_window(reference_date=today, booking=b).
    - Generate an invoice only if today == window['generate_on'].
    - Use cycle_month = window['start'] and due_date = window['end'].
    """
    today = timezone.localdate()

    # Active bookings overlapping today
    active_qs = (
        Booking.objects.filter(status="confirmed")
        .filter(Q(start_date__isnull=True) | Q(start_date__lte=today))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .select_related("tenant", "room", "building", "building__owner")
    )

    created = 0
    for b in active_qs:
        # Settings resolution: building override then owner-global
        owner = getattr(getattr(b, "building", None), "owner", None)
        if not owner or not getattr(b, "booked_at", None):
            continue
        settings_obj = (
            InvoiceSettings.objects.filter(owner=owner, building_id=getattr(b, "building_id", None)).first()
            or InvoiceSettings.objects.filter(owner=owner, building__isnull=True).first()
        )

        # If no record, mimic model defaults
        if not settings_obj:
            settings_obj = InvoiceSettings(
                owner=owner,
                building=None,
                generate_type=InvoiceSettings.GenerateType.AUTOMATIC,
                period=InvoiceSettings.Period.MONTHLY,
                generate_on=InvoiceSettings.GenerateOn.START,
                monthly_cycle=InvoiceSettings.MonthlyCycle.CHECKIN_DATE,
            )

        # Only automatic + monthly are handled by this task
        if (
            settings_obj.generate_type != InvoiceSettings.GenerateType.AUTOMATIC
            or settings_obj.period != InvoiceSettings.Period.MONTHLY
        ):
            continue

        # Compute period window for this booking at today's month
        window = settings_obj.monthly_period_window(reference_date=today, booking=b)
        start = window.get("start")
        end = window.get("end")
        gen_on = window.get("generate_on")
        if not start or not end or not gen_on or gen_on != today:
            continue

        # Avoid duplicates for the same booking + cycle start
        try:
            start_first = Invoice._first_day_of_month(start)
        except Exception:
            start_first = start
        if Invoice.objects.filter(booking=b, cycle_month=start_first).exists():
            continue

        base = (b.monthly_rent or Decimal("0")) + (b.maintenance_amount or Decimal("0")) - (b.discount_amount or Decimal("0"))
        if base < 0:
            base = Decimal("0.00")

        with transaction.atomic():
            inv = Invoice(
                booking=b,
                cycle_month=start_first,
                issue_date=today,
                due_date=end,
                amount=base,
                tax_amount=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                notes=f"Auto-generated for period {start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}",
                status=Invoice.Status.OPEN,
            )
            inv.save()
            created += 1
    return created


@shared_task(name="payment.tasks.mark_overdue_invoices")
def mark_overdue_invoices() -> int:
    """Mark open/partial invoices as OVERDUE if due_date has passed (local date)."""
    today = timezone.localdate()
    qs = Invoice.objects.filter(status__in=[Invoice.Status.OPEN, Invoice.Status.PARTIAL], due_date__lt=today)
    updated = qs.update(status=Invoice.Status.OVERDUE, updated_at=timezone.now())
    return updated
