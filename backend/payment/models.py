from django.db import models
from django.db.models import Max
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify
from django.db.models.functions import Lower
from decimal import Decimal
from datetime import date as _date
from django.core.exceptions import ValidationError
from properties.models import TimeStampedModel
from django.conf import settings
from django.db import transaction
from django.core.validators import MinValueValidator, MaxValueValidator
from tenants.models import validate_file_size


class Invoice(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        PARTIAL = "partial", "Partially Paid"
        PAID = "paid", "Paid"
        OVERDUE = "overdue", "Overdue"
        VOID = "void", "Void"

    # Link to booking (assumes bookings app has Booking model)
    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.CASCADE, related_name="invoices"
    )

    # Cycle anchor: date within the month for which this invoice applies (anchored to booking confirmation day)
    cycle_month = models.DateField(help_text="Cycle date for the billed month (YYYY-MM-DD), defaults to booking confirmation day")

    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField()

    # Amounts
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    balance_due = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)

    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["booking", "cycle_month"], name="uniq_invoice_booking_cyclemonth"
            ),
            models.CheckConstraint(
                check=models.Q(amount__gte=0)
                & models.Q(tax_amount__gte=0)
                & models.Q(discount_amount__gte=0)
                & models.Q(total_amount__gte=0)
                & models.Q(balance_due__gte=0),
                name="invoice_non_negative_amounts",
            ),
        ]
        indexes = [
            models.Index(fields=["booking"], name="idx_invoice_booking"),
            models.Index(fields=["cycle_month"], name="idx_invoice_cycle_month"),
            models.Index(fields=["status"], name="idx_invoice_status"),
        ]
        ordering = ["-cycle_month", "-created_at"]

    def __str__(self) -> str:
        return f"Invoice #{self.pk} • Booking {self.booking_id} • {self.cycle_month} • {self.status}"

    # --- Helpers for cycle derivation ---
    @staticmethod
    def _first_day_of_month(d: _date) -> _date:
        return _date(d.year, d.month, 1) if d else None

    @staticmethod
    def _clamp_day(year: int, month: int, day: int) -> _date:
        """Return a date with the same day if possible, otherwise clamp to last day of month."""
        from calendar import monthrange
        last = monthrange(year, month)[1]
        safe_day = max(1, min(day, last))
        return _date(year, month, safe_day)

    @staticmethod
    def _checkin_anchor_for_booking(booking) -> _date | None:
        """
        Derive the monthly anchor from booking.start_date (check-in) with rules:
        - If check-in day is 1 -> anchor to last day of that month
        - If check-in day >= last day of the month -> anchor to last day
        - Else anchor to same day-of-month
        """
        try:
            start = getattr(booking, "start_date", None)
            if not start:
                return None
            from calendar import monthrange
            last = monthrange(start.year, start.month)[1]
            day = start.day
            if day == 1 or day >= last:
                # Anchor to month end
                return _date(start.year, start.month, last)
            return _date(start.year, start.month, day)
        except Exception:
            return None

    def _derive_cycle_anchor(self):
        """Prefer check-in based anchor; fallback to booking confirmation local date."""
        anchor = None
        if getattr(self, "booking_id", None):
            b = self.booking
            # First try check-in rules from start_date
            anchor = Invoice._checkin_anchor_for_booking(b)
            if not anchor and getattr(b, "status", None) == "confirmed" and getattr(b, "booked_at", None):
                anchor = timezone.localtime(b.booked_at).date()
        return anchor

    # --- Validation for dynamic cycle selection ---
    def clean(self):
        super().clean()
        # Booking must be confirmed with a confirmation timestamp
        b = getattr(self, "booking", None)
        if not b or getattr(b, "status", None) != "confirmed" or not getattr(b, "booked_at", None):
            raise ValidationError({"booking": "Invoice requires a confirmed booking with confirmation date."})

        # If a cycle_month is set explicitly, validate it is within allowed window
        if self.cycle_month:
            anchor_date = self._derive_cycle_anchor()
            # Upper bound: booking.end_date (if set) else today's date
            upper_bound_date = getattr(b, "end_date", None) or timezone.localdate()
            if self.cycle_month < Invoice._first_day_of_month(anchor_date):
                raise ValidationError({"cycle_month": "Cannot be before booking confirmation month."})
            if self.cycle_month > Invoice._first_day_of_month(upper_bound_date):
                raise ValidationError({"cycle_month": "Cannot be after the allowed upper bound (booking end or current month)."})

            # Preempt uniqueness by checking if another invoice exists for same booking+month
            exists = (
                Invoice.objects.filter(booking_id=b.id, cycle_month=self.cycle_month)
                .exclude(pk=self.pk)
                .exists()
            )
            if exists:
                raise ValidationError({"cycle_month": "An invoice for this booking and month already exists."})

        # Validate due/issue date ordering
        if getattr(self, "due_date", None) and getattr(self, "issue_date", None):
            if self.due_date < self.issue_date:
                raise ValidationError({"due_date": "Due date cannot be before the issue date."})

        # Validate discount does not exceed subtotal + tax to avoid negative totals
        amt = (self.amount or Decimal("0.00")) + (self.tax_amount or Decimal("0.00"))
        if amt < (self.discount_amount or Decimal("0.00")):
            raise ValidationError({"discount_amount": "Discount cannot exceed subtotal plus tax."})

    # --- Utilities to support UI with selectable cycle months ---
    @staticmethod
    def _month_add(d: _date, months: int = 1) -> _date:
        y, m = d.year, d.month + months
        y += (m - 1) // 12
        m = ((m - 1) % 12) + 1
        return Invoice._clamp_day(y, m, d.day)

    @classmethod
    def cycle_month_options_for_booking(
        cls,
        booking,
        include_existing: bool = False,
        until_date: _date | None = None,
    ) -> list[_date]:
        """Return list of cycle dates eligible for invoicing for a booking.

        - Starts at the booking confirmation month/day (localized booked_at).
        - Ends at booking.end_date month (if present) else current month/day.
        - By default excludes dates already invoiced for that booking.
        """
        if not booking or getattr(booking, "status", None) != "confirmed":
            return []

        # Use check-in anchor if possible; else fall back to booked_at date
        start = cls._checkin_anchor_for_booking(booking) or (
            timezone.localtime(booking.booked_at).date() if getattr(booking, "booked_at", None) else None
        )
        if not start:
            return []

        end = until_date or getattr(booking, "end_date", None) or timezone.localdate()

        # Build set of existing months if we need to exclude
        existing = set()
        if not include_existing:
            existing = set(
                cls.objects.filter(booking_id=booking.id).values_list("cycle_month", flat=True)
            )

        # Iterate months inclusively from start to end
        options: list[_date] = []
        cur = start
        while cur <= end:
            if include_existing or cur not in existing:
                options.append(cur)
            cur = cls._month_add(cur, 1)
        return options

    # --- Lifecycle overrides ---
    def save(self, *args, **kwargs):
        """Default cycle date to booking confirmation date; keep totals consistent."""
        # Default cycle_month to booking confirmation local date if not provided
        cm = getattr(self, "cycle_month", None)
        if not cm:
            anchor = self._derive_cycle_anchor()
            if not anchor:
                raise ValidationError({"booking": "Invoice can only be created/processed when booking is confirmed with a confirmation date."})
            self.cycle_month = anchor

        # Enforce booking is confirmed even if cycle_month supplied explicitly
        try:
            if getattr(self, "booking_id", None):
                b = self.booking
                if getattr(b, "status", None) != "confirmed" or not getattr(b, "booked_at", None):
                    raise ValidationError({"booking": "Invoice can only be created/processed when booking is confirmed with a confirmation date."})
        except Exception:
            # If booking cannot be accessed, also prevent save
            raise ValidationError({"booking": "Invalid or inaccessible booking for invoice creation."})

        # Normalize cycle_month to first day of the month for consistency
        if getattr(self, "cycle_month", None):
            self.cycle_month = Invoice._first_day_of_month(self.cycle_month)

        # Keep totals consistent always; recalc_totals initializes balance_due only on create
        try:
            self.recalc_totals()
        except Exception:
            pass

        # Validate then save
        self.full_clean(exclude=None)
        return super().save(*args, **kwargs)

    def open(self):
        if self.status == self.Status.DRAFT:
            self.status = self.Status.OPEN
            self.save(update_fields=["status", "updated_at"])

    def recalc_totals(self):
        """Ensure total_amount and balance_due are consistent."""
        self.total_amount = (self.amount + self.tax_amount) - self.discount_amount
        # Initialize balance_due on create
        if self._state.adding:
            self.balance_due = self.total_amount

    def apply_payment_amount(self, pay_amount: Decimal):
        """Apply a payment to this invoice (call within a transaction)."""
        if pay_amount <= 0:
            return
        new_balance = (self.balance_due or Decimal("0.00")) - Decimal(pay_amount)
        self.balance_due = max(new_balance, Decimal("0.00"))
        if self.balance_due == 0:
            self.status = self.Status.PAID
        elif self.status in {self.Status.DRAFT, self.Status.OPEN}:
            self.status = self.Status.PARTIAL
        self.save(update_fields=["balance_due", "status", "updated_at"])

    def adjust_payment_delta(self, delta: Decimal):
        """Adjust balance due when an existing payment's amount changes.

        Positive delta => more paid now; reduce balance_due.
        Negative delta => reduce previously paid; increase balance_due.
        """
        if not delta:
            return
        current = self.balance_due or Decimal("0.00")
        new_balance = current - Decimal(delta)
        # Do not allow negative balance due
        self.balance_due = max(new_balance, Decimal("0.00"))
        if self.balance_due == 0:
            self.status = self.Status.PAID
        else:
            # If some due remains, ensure not PAID/DRAFT/OPEN depending on prior
            if self.status in {self.Status.DRAFT, self.Status.OPEN, self.Status.PAID}:
                self.status = self.Status.PARTIAL
        self.save(update_fields=["balance_due", "status", "updated_at"])


class Payment(TimeStampedModel):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        BANK = "bank", "Bank Transfer"
        OTHER = "other", "Other"

    # Optional link to invoice (can be null for standalone payments)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments", null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    # Make method dynamic/optional. Validation of allowed methods handled at serializer level.
    method = models.CharField(max_length=32, blank=True, null=True, help_text="Payment method (validated in serializer via settings.PAYMENT_METHOD_CHOICES)")
    reference = models.CharField(max_length=128, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    notes = models.TextField(blank=True)

    # Override TimeStampedModel relations to avoid reverse accessor clashes with bookings.Payment
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="paymentapp_payment_created",
        help_text="User who created this record",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="paymentapp_payment_updated",
        help_text="User who last updated this record",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(amount__gt=0), name="payment_amount_positive"),
        ]
        indexes = [
            models.Index(fields=["invoice"], name="idx_payment_invoice"),
            models.Index(fields=["received_at"], name="idx_payment_received_at"),
        ]
        ordering = ["-received_at", "-id"]

    def __str__(self) -> str:
        inv_part = f"Invoice {self.invoice_id}" if getattr(self, "invoice_id", None) else "No Invoice"
        return f"Payment #{self.pk} • {inv_part} • {self.amount}"

    def save(self, *args, **kwargs):
        is_create = self._state.adding
        # Capture old amount for delta computation on update
        old_amount = None
        old_invoice_id = None
        if not is_create and getattr(self, 'pk', None):
            try:
                prev = Payment.objects.get(pk=self.pk)
                old_amount = Decimal(prev.amount)
                old_invoice_id = prev.invoice_id
            except Payment.DoesNotExist:
                old_amount = None
                old_invoice_id = None
        with transaction.atomic():
            super().save(*args, **kwargs)
            if getattr(self, "invoice_id", None):
                inv = Invoice.objects.select_for_update().get(pk=self.invoice_id)
                if is_create:
                    # Apply freshly created payment
                    inv.apply_payment_amount(Decimal(self.amount))
                else:
                    # If invoice unchanged, adjust by delta; if invoice changed, reverse old then apply new
                    if old_invoice_id == self.invoice_id:
                        if old_amount is not None:
                            delta = Decimal(self.amount) - old_amount
                            if delta:
                                inv.adjust_payment_delta(delta)
                    else:
                        # Reverse from old invoice
                        if old_invoice_id:
                            old_inv = Invoice.objects.select_for_update().get(pk=old_invoice_id)
                            # Moving payment: add back old amount to old invoice
                            old_inv.adjust_payment_delta(Decimal(0) - (old_amount or Decimal("0.00")))
                        # Apply full new amount to new invoice
                        inv.adjust_payment_delta(Decimal(self.amount))


# --- Operational Expenses (not tied to a specific invoice) ---
class ExpenseCategory(TimeStampedModel):
    name = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)
    # Owner pg_admin for scoping categories per account
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="paymentapp_expensecategory_owner",
        help_text="PG Admin who owns this category",
    )
    # Per-admin running number; unique within the owner
    sequence = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            # Ensure case-insensitive uniqueness per owner (owner can be NULL for global categories)
            models.UniqueConstraint(Lower("name"), "owner", name="uniq_expensecategory_owner_name_ci"),
            models.UniqueConstraint(
                fields=['owner', 'sequence'],
                name='unique_expensecategory_sequence_per_owner',
            ),
        ]
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        # Normalize: strip and collapse spaces, Title Case (consistent with seeded names)
        if self.name:
            norm = " ".join(str(self.name).strip().split())
            self.name = norm
        # Auto-assign per-admin sequence on first save
        if self.owner_id and not self.sequence:
            # Find next sequence for this owner
            last = ExpenseCategory.objects.filter(owner_id=self.owner_id).aggregate(mx=Max('sequence'))['mx']
            self.sequence = (last or 0) + 1
        super().save(*args, **kwargs)

    @property
    def display_code(self) -> str:
        """Returns '<owner_id>.<sequence>' if available, else empty string."""
        if self.owner_id and self.sequence:
            return f"{self.owner_id}.{self.sequence}"
        return ""

    def __str__(self) -> str:
        code = self.display_code
        return f"{code} - {self.name}" if code else self.name


class Expense(TimeStampedModel):
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    category = models.CharField(max_length=50, default="other")
    expense_date = models.DateField(default=timezone.localdate)
    description = models.TextField(blank=True)
    reference = models.CharField(max_length=128, blank=True, help_text="Receipt/Bill reference")
    building = models.ForeignKey(
        "properties.Building",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses",
        help_text="Optional: attribute expense to a building",
    )
    attachment = models.FileField(
        upload_to="expenses/attachments/",
        null=True,
        blank=True,
        validators=[validate_file_size],
    )
    metadata = models.JSONField(default=dict, blank=True)
    # User stamps
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="paymentapp_expense_created",
        help_text="User who created this record",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="paymentapp_expense_updated",
        help_text="User who last updated this record",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(amount__gt=0), name="expense_amount_positive"),
        ]
        indexes = [
            models.Index(fields=["category", "expense_date"], name="idx_expense_category_date"),
            models.Index(fields=["building"], name="idx_expense_building"),
        ]
        ordering = ["-expense_date", "-created_at"]

    def __str__(self) -> str:
        b = getattr(self.building, "name", None)
        return f"Expense ₹{self.amount} • {self.category} • {self.expense_date}{' • ' + b if b else ''}"


# --- Per-invoice expenses (line items) ---
class InvoiceExpense(TimeStampedModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="expenses")
    label = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    taxable = models.BooleanField(default=False)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"), help_text="Percent, e.g. 18 for 18%")
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(amount__gte=0), name="invoice_expense_non_negative"),
            models.CheckConstraint(check=models.Q(tax_rate__gte=0), name="invoice_expense_taxrate_non_negative"),
        ]
        indexes = [
            models.Index(fields=["invoice"], name="idx_invoice_expense_invoice"),
        ]
        ordering = ["invoice", "-created_at"]

    def __str__(self) -> str:
        return f"InvoiceExpense #{self.pk} • Invoice {self.invoice_id} • {self.label} • {self.amount}"


# --- Invoice Settings (per PG admin, optional per-building override) ---
class InvoiceSettings(TimeStampedModel):
    class GenerateType(models.TextChoices):
        MANUAL = "manual", "Manual"
        AUTOMATIC = "automatic", "Automatic"

    class Period(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    class MonthlyCycle(models.TextChoices):
        CALENDAR_MONTH = "calendar_month", "Calendar Month"
        CHECKIN_DATE = "checkin_date", "Check-in Date"
        CUSTOM_DAY = "custom_day", "Custom Day"

    class WeeklyCycle(models.TextChoices):
        CALENDAR_WEEK = "calendar_week", "Calendar Week"
        CHECKIN_DATE = "checkin_date", "Check-in Date"
        CUSTOM_DAY = "custom_day", "Custom Day"

    class GenerateOn(models.TextChoices):
        START = "start", "Start of Period"
        END = "end", "End of Period"

    # Scope
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paymentapp_invoicesettings_owner",
        help_text="PG Admin owner of these settings",
    )
    building = models.ForeignKey(
        "properties.Building",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="invoice_settings",
        help_text="Optional: override settings for a specific building",
    )

    # Core options
    generate_type = models.CharField(max_length=16, choices=GenerateType.choices, default=GenerateType.AUTOMATIC)
    period = models.CharField(max_length=16, choices=Period.choices, default=Period.MONTHLY)
    generate_on = models.CharField(max_length=8, choices=GenerateOn.choices, default=GenerateOn.START)

    # Monthly configuration
    monthly_cycle = models.CharField(max_length=20, choices=MonthlyCycle.choices, default=MonthlyCycle.CHECKIN_DATE)
    monthly_custom_day = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(31)],
        help_text="Required when monthly_cycle=custom_day (1-31)",
    )

    # Weekly configuration (0=Monday .. 6=Sunday per Python datetime.weekday)
    weekly_cycle = models.CharField(max_length=20, choices=WeeklyCycle.choices, default=WeeklyCycle.CHECKIN_DATE)
    weekly_custom_weekday = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(6)],
        help_text="Required when weekly_cycle=custom_day (0=Mon .. 6=Sun)",
    )

    notes = models.TextField(blank=True, help_text="Optional internal notes")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "building"],
                name="uniq_invoicesettings_owner_building",
            ),
        ]
        indexes = [
            models.Index(fields=["owner"], name="idx_invoicesettings_owner"),
            models.Index(fields=["building"], name="idx_invoicesettings_building"),
        ]
        ordering = ["owner_id", "building_id", "-created_at"]

    def __str__(self) -> str:
        scope = f"Building {self.building_id}" if self.building_id else "Global"
        return f"InvoiceSettings • Owner {self.owner_id} • {scope}"

    def clean(self):
        super().clean()
        # Monthly custom day required iff monthly_cycle is custom_day
        if self.monthly_cycle == self.MonthlyCycle.CUSTOM_DAY and not self.monthly_custom_day:
            raise ValidationError({"monthly_custom_day": "This field is required when monthly_cycle=custom_day."})
        if self.monthly_cycle != self.MonthlyCycle.CUSTOM_DAY and self.monthly_custom_day:
            # Normalize: unset when not used
            self.monthly_custom_day = None
        # Weekly custom weekday required iff weekly_cycle is custom_day
        if self.weekly_cycle == self.WeeklyCycle.CUSTOM_DAY and self.weekly_custom_weekday is None:
            raise ValidationError({"weekly_custom_weekday": "This field is required when weekly_cycle=custom_day."})
        if self.weekly_cycle != self.WeeklyCycle.CUSTOM_DAY and self.weekly_custom_weekday is not None:
            self.weekly_custom_weekday = None
        # Owner role expectation is enforced at view level; ensure owner exists
        if not getattr(self, "owner_id", None):
            raise ValidationError({"owner": "Owner is required."})

    # --- Period computation helpers (for UI/jobs) ---
    def _today(self) -> _date:
        try:
            return timezone.localdate()
        except Exception:
            return _date.today()

    def _calc_monthly_custom_window(self, ref: _date) -> tuple[_date, _date]:
        """
        For monthly_cycle=custom_day with monthly_custom_day = D (1..31):
        - Start = clamp(ref.year, ref.month, D)
        - End = Start shifted by +1 month, clamped to that month's D
        This yields periods like 15th–15th next month (inclusive bounds semantics up to the app).
        """
        day = int(self.monthly_custom_day or 1)
        start = Invoice._clamp_day(ref.year, ref.month, day)
        end = Invoice._month_add(start, 1)
        return start, end

    def monthly_period_window(self, reference_date: _date | None = None, booking=None) -> dict:
        """
        Compute monthly period window and generation date according to settings.

        Returns dict: { 'start': date, 'end': date, 'generate_on': date }

        Guarantees the rule requested by frontend: for
        Monthly + Custom day X + End => period Xth–Xth (next month) and generate_on = Xth (end).
        """
        ref = reference_date or self._today()
        start: _date | None = None
        end: _date | None = None

        if self.monthly_cycle == self.MonthlyCycle.CUSTOM_DAY and self.monthly_custom_day:
            start, end = self._calc_monthly_custom_window(ref)
        elif self.monthly_cycle == self.MonthlyCycle.CHECKIN_DATE and getattr(booking, 'start_date', None):
            # Derive anchor from booking and then build a window [anchor .. anchor+1month)
            anchor = Invoice._checkin_anchor_for_booking(booking)
            # Align to current month of ref by replacing year/month from ref
            if anchor:
                aligned = Invoice._clamp_day(ref.year, ref.month, anchor.day)
                start = aligned
                end = Invoice._month_add(aligned, 1)
        else:
            # Calendar month fallback: 1st .. 1st of next month
            first = _date(ref.year, ref.month, 1)
            start = first
            end = Invoice._month_add(first, 1)

        if not start or not end:
            # Final fallback to a 1-day window on ref to avoid crashes
            start = ref
            end = Invoice._month_add(ref, 1)

        gen_date = start if self.generate_on == self.GenerateOn.START else end
        return { 'start': start, 'end': end, 'generate_on': gen_date }
