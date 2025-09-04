from django.shortcuts import render
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from django.db.models import Q
from functools import reduce
import operator
from .signals import DEFAULT_EXPENSE_CATEGORIES
from accounts.permissions import ensure_staff_module_permission
from accounts.models import log_activity

from .models import Invoice, Payment, Expense, InvoiceExpense, ExpenseCategory, InvoiceSettings
from .serializers import (
    InvoiceSerializer,
    PaymentSerializer,
    ExpenseSerializer,
    InvoiceExpenseSerializer,
    ExpenseCategorySerializer,
    InvoiceSettingsSerializer,
)

from rest_framework.views import APIView
from django.db.models import Sum
from collections import defaultdict
import calendar
from datetime import timedelta
from django.db.models.functions import ExtractYear, ExtractMonth, ExtractIsoWeekDay
import logging

logger = logging.getLogger(__name__)

# Create your views here.

class InvoiceViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = (
        Invoice.objects.select_related(
            "booking",
            "booking__tenant",
            "booking__building",
            "booking__floor",
            "booking__room",
            "booking__bed",
        ).all()
    )
    serializer_class = InvoiceSerializer
    ordering = ("-cycle_month", "-created_at")

    def get_queryset(self):
        qs = super().get_queryset()
        # Ownership isolation: superuser all, pg_admin own, pg_staff their admin's
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not getattr(user, "is_superuser", False):
            role = getattr(user, "role", None)
            if role == "pg_admin":
                qs = qs.filter(booking__building__owner=user)
            elif role == "pg_staff" and getattr(user, "pg_admin_id", None):
                qs = qs.filter(booking__building__owner_id=user.pg_admin_id)
                # Apply staff 'view' permission: allow global or building-scoped
                params = self.request.query_params
                building = params.get("building")
                building_in = params.get("building__in")
                # If client specifies buildings, enforce per-building permission
                if building:
                    try:
                        b_id = int(building)
                    except Exception:
                        return qs.none()
                    if not ensure_staff_module_permission(user, 'invoices', 'view', building_id=b_id):
                        # no scoped view, also check global fallback
                        if not ensure_staff_module_permission(user, 'invoices', 'view'):
                            return qs.none()
                    qs = qs.filter(booking__building_id=b_id)
                elif building_in:
                    try:
                        req_ids = [int(x) for x in str(building_in).split(',') if str(x).strip()]
                    except Exception:
                        req_ids = []
                    allowed = []
                    for b in req_ids:
                        if ensure_staff_module_permission(user, 'invoices', 'view', building_id=b):
                            allowed.append(b)
                    if not allowed:
                        # fall back to global view
                        if not ensure_staff_module_permission(user, 'invoices', 'view'):
                            return qs.none()
                    else:
                        qs = qs.filter(booking__building_id__in=allowed)
                else:
                    # No explicit building filter; restrict to buildings where staff has view
                    perms = getattr(user, 'permissions', {}) or {}
                    allowed_ids = []
                    for key, scope in (perms.items() if isinstance(perms, dict) else []):
                        if key == 'global':
                            continue
                        try:
                            b = int(key)
                        except Exception:
                            continue
                        mod = (scope or {}).get('invoices', {}) or {}
                        if isinstance(mod.get('view'), bool) and mod.get('view'):
                            allowed_ids.append(b)
                    if allowed_ids:
                        qs = qs.filter(booking__building_id__in=allowed_ids)
                    else:
                        # Only allow if global view is granted
                        if not ensure_staff_module_permission(user, 'invoices', 'view'):
                            return qs.none()
            else:
                return qs.none()
        params = self.request.query_params
        booking = params.get("booking")
        tenant = params.get("tenant")
        status_param = params.get("status")
        status_in_param = params.get("status__in")
        cycle_month = params.get("cycle_month")  # YYYY-MM-01 format expected
        # New: building filters
        building = params.get("building")
        building_in = params.get("building__in")
        # New: special flags and date ranges
        pending_flag = params.get("pending")
        overdue_flag = params.get("overdue")
        issue_gte = params.get("issue_date__gte")
        issue_lte = params.get("issue_date__lte")

        if booking:
            qs = qs.filter(booking_id=booking)
        if tenant:
            qs = qs.filter(booking__tenant_id=tenant)
        # Status / pending / overdue filters
        q_parts = []
        statuses = None
        if status_in_param:
            try:
                statuses = [s.strip().lower() for s in str(status_in_param).split(',') if s.strip()]
            except Exception:
                statuses = None
        if not statuses and status_param:
            statuses = [str(status_param).strip().lower()]
        if statuses:
            q_parts.append(Q(status__in=statuses))
        # pending = any invoice with balance_due > 0
        if isinstance(pending_flag, str) and pending_flag.lower() in {"1", "true", "yes"}:
            q_parts.append(Q(balance_due__gt=0))
        # overdue = balance_due > 0 AND due_date < today
        if isinstance(overdue_flag, str) and overdue_flag.lower() in {"1", "true", "yes"}:
            q_parts.append(Q(balance_due__gt=0, due_date__lt=timezone.localdate()))
        if q_parts:
            qs = qs.filter(reduce(operator.or_, q_parts))
        if cycle_month:
            # Allow exact match on stored date
            qs = qs.filter(cycle_month=cycle_month)
        # Issue date range
        d_from = parse_date(issue_gte) if issue_gte else None
        d_to = parse_date(issue_lte) if issue_lte else None
        if d_from:
            qs = qs.filter(issue_date__gte=d_from)
        if d_to:
            qs = qs.filter(issue_date__lte=d_to)
        # Apply building filters
        if building:
            qs = qs.filter(booking__building_id=building)
        if building_in:
            try:
                ids = [int(x) for x in str(building_in).split(',') if str(x).strip()]
                if ids:
                    qs = qs.filter(booking__building_id__in=ids)
            except Exception:
                pass
        return qs

    def perform_create(self, serializer):
        user = getattr(self.request, 'user', None)
        if not user or not user.is_authenticated:
            raise PermissionDenied('Authentication required')
        role = getattr(user, 'role', None)
        booking = serializer.validated_data.get('booking')
        if not booking:
            raise PermissionDenied('Booking is required to create invoice')
        owner_id = getattr(booking.building, 'owner_id', None)
        if getattr(user, 'is_superuser', False):
            instance = serializer.save()
            try:
                owner = getattr(booking.building, 'owner', None)
                meta = {
                    'type': 'invoice',
                    'module': 'invoices',
                    'invoice': {
                        'id': instance.id,
                        'status': instance.status,
                        'balance_due': str(instance.balance_due),
                        'due_date': instance.due_date.isoformat() if instance.due_date else None,
                    },
                    'booking_id': instance.booking_id,
                    'tenant_id': getattr(instance.booking, 'tenant_id', None),
                    'building_id': getattr(instance.booking, 'building_id', None),
                    'route': f"/invoices/{instance.id}",
                }
                # Log for actor and owner (if different)
                log_activity(user, 'create', description='Invoice created', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'create', description='Invoice created', meta=meta)
            except Exception:
                pass
            return
        if role == 'pg_admin':
            if owner_id != getattr(user, 'id', None):
                raise PermissionDenied('You can only create invoices for your buildings.')
            instance = serializer.save()
            try:
                owner = getattr(booking.building, 'owner', None)
                meta = {
                    'type': 'invoice',
                    'module': 'invoices',
                    'invoice': {
                        'id': instance.id,
                        'status': instance.status,
                        'balance_due': str(instance.balance_due),
                        'due_date': instance.due_date.isoformat() if instance.due_date else None,
                    },
                    'booking_id': instance.booking_id,
                    'tenant_id': getattr(instance.booking, 'tenant_id', None),
                    'building_id': getattr(instance.booking, 'building_id', None),
                    'route': f"/invoices/{instance.id}",
                }
                log_activity(user, 'create', description='Invoice created', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'create', description='Invoice created', meta=meta)
            except Exception:
                pass
            return
        if role == 'pg_staff':
            if owner_id != getattr(user, 'pg_admin_id', None):
                raise PermissionDenied('You can only create invoices within your PG Admin\'s buildings.')
            if not ensure_staff_module_permission(user, 'invoices', 'add', building_id=getattr(booking, 'building_id', None)):
                raise PermissionDenied('You do not have permission to add invoices.')
            instance = serializer.save()
            try:
                owner = getattr(booking.building, 'owner', None)
                meta = {
                    'type': 'invoice',
                    'module': 'invoices',
                    'invoice': {
                        'id': instance.id,
                        'status': instance.status,
                        'balance_due': str(instance.balance_due),
                        'due_date': instance.due_date.isoformat() if instance.due_date else None,
                    },
                    'booking_id': instance.booking_id,
                    'tenant_id': getattr(instance.booking, 'tenant_id', None),
                    'building_id': getattr(instance.booking, 'building_id', None),
                    'route': f"/invoices/{instance.id}",
                }
                # Log to staff actor and the admin owner so both see it
                log_activity(user, 'create', description='Invoice created', meta=meta)
                if owner:
                    log_activity(owner, 'create', description='Invoice created', meta=meta)
            except Exception:
                pass
            return
        raise PermissionDenied('You are not allowed to create invoices.')

    def perform_update(self, serializer):
        user = getattr(self.request, 'user', None)
        instance: Invoice = self.get_object()
        if getattr(user, 'is_superuser', False):
            instance = serializer.save()
            try:
                owner = getattr(instance.booking.building, 'owner', None)
                meta = {
                    'type': 'invoice',
                    'module': 'invoices',
                    'invoice': {
                        'id': instance.id,
                        'status': instance.status,
                        'balance_due': str(instance.balance_due),
                        'due_date': instance.due_date.isoformat() if instance.due_date else None,
                    },
                    'booking_id': instance.booking_id,
                    'tenant_id': getattr(instance.booking, 'tenant_id', None),
                    'building_id': getattr(instance.booking, 'building_id', None),
                    'route': f"/invoices/{instance.id}",
                }
                log_activity(user, 'update', description='Invoice updated', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'update', description='Invoice updated', meta=meta)
            except Exception:
                pass
            return
        role = getattr(user, 'role', None)
        # Determine target booking owner after potential booking change
        target_booking = serializer.validated_data.get('booking', instance.booking)
        owner_id = getattr(target_booking.building, 'owner_id', None)
        if role == 'pg_admin' and owner_id == getattr(user, 'id', None):
            instance = serializer.save()
            try:
                owner = getattr(instance.booking.building, 'owner', None)
                meta = {
                    'type': 'invoice',
                    'module': 'invoices',
                    'invoice': {
                        'id': instance.id,
                        'status': instance.status,
                        'balance_due': str(instance.balance_due),
                        'due_date': instance.due_date.isoformat() if instance.due_date else None,
                    },
                    'booking_id': instance.booking_id,
                    'tenant_id': getattr(instance.booking, 'tenant_id', None),
                    'building_id': getattr(instance.booking, 'building_id', None),
                    'route': f"/invoices/{instance.id}",
                }
                log_activity(user, 'update', description='Invoice updated', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'update', description='Invoice updated', meta=meta)
            except Exception:
                pass
            return
        if role == 'pg_staff' and owner_id == getattr(user, 'pg_admin_id', None):
            if not ensure_staff_module_permission(user, 'invoices', 'edit', building_id=getattr(target_booking, 'building_id', None)):
                raise PermissionDenied('You do not have permission to edit invoices.')
            instance = serializer.save()
            try:
                owner = getattr(instance.booking.building, 'owner', None)
                meta = {
                    'type': 'invoice',
                    'module': 'invoices',
                    'invoice': {
                        'id': instance.id,
                        'status': instance.status,
                        'balance_due': str(instance.balance_due),
                        'due_date': instance.due_date.isoformat() if instance.due_date else None,
                    },
                    'booking_id': instance.booking_id,
                    'tenant_id': getattr(instance.booking, 'tenant_id', None),
                    'building_id': getattr(instance.booking, 'building_id', None),
                    'route': f"/invoices/{instance.id}",
                }
                log_activity(user, 'update', description='Invoice updated', meta=meta)
                if owner:
                    log_activity(owner, 'update', description='Invoice updated', meta=meta)
            except Exception:
                pass
            return
        raise PermissionDenied('You cannot modify this invoice.')

    @action(detail=True, methods=["post"], url_path="open")
    def open_invoice(self, request, pk=None):
        invoice: Invoice = self.get_object()
        # Permission: superuser ok; pg_admin must own; pg_staff needs 'edit' invoices for that building
        user = request.user
        if not getattr(user, 'is_superuser', False):
            role = getattr(user, 'role', None)
            owner_id = getattr(invoice.booking.building, 'owner_id', None)
            if role == 'pg_admin':
                if owner_id != getattr(user, 'id', None):
                    raise PermissionDenied('You cannot modify this invoice.')
            elif role == 'pg_staff':
                if owner_id != getattr(user, 'pg_admin_id', None):
                    raise PermissionDenied('You cannot modify this invoice.')
                if not ensure_staff_module_permission(user, 'invoices', 'edit', building_id=getattr(invoice.booking, 'building_id', None)):
                    raise PermissionDenied('You do not have permission to edit invoices.')
            else:
                raise PermissionDenied('You cannot modify this invoice.')
        invoice.open()
        try:
            owner = getattr(invoice.booking.building, 'owner', None)
            meta = {
                'type': 'invoice',
                'module': 'invoices',
                'invoice': {
                    'id': invoice.id,
                    'status': invoice.status,
                    'balance_due': str(invoice.balance_due),
                    'due_date': invoice.due_date.isoformat() if invoice.due_date else None,
                },
                'booking_id': invoice.booking_id,
                'tenant_id': getattr(invoice.booking, 'tenant_id', None),
                'building_id': getattr(invoice.booking, 'building_id', None),
                'route': f"/invoices/{invoice.id}",
            }
            log_activity(user, 'update', description='Invoice opened', meta=meta)
            if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                log_activity(owner, 'update', description='Invoice opened', meta=meta)
        except Exception:
            pass
        return Response(self.get_serializer(invoice).data)

class PaymentViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = Payment.objects.select_related("invoice", "created_by", "updated_by").all()
    serializer_class = PaymentSerializer
    ordering = ("-received_at", "-id")

    def get_queryset(self):
        qs = super().get_queryset()
        # Ownership isolation via invoice.booking.building.owner
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not getattr(user, "is_superuser", False):
            role = getattr(user, "role", None)
            if role == "pg_admin":
                qs = qs.filter(invoice__booking__building__owner=user)
            elif role == "pg_staff" and getattr(user, "pg_admin_id", None):
                qs = qs.filter(invoice__booking__building__owner_id=user.pg_admin_id)
                # Respect building-scoped 'view' permission
                params = self.request.query_params
                building = params.get("building") or params.get("building_id")
                building_in = params.get("building__in") or params.get("building_id__in") or params.get("building_ids")
                if building:
                    try:
                        b_id = int(building)
                    except Exception:
                        return qs.none()
                    if not ensure_staff_module_permission(user, 'payments', 'view', building_id=b_id):
                        if not ensure_staff_module_permission(user, 'payments', 'view'):
                            return qs.none()
                    qs = qs.filter(invoice__booking__building_id=b_id)
                elif building_in:
                    try:
                        req_ids = [int(x) for x in str(building_in).split(',') if str(x).strip()]
                    except Exception:
                        req_ids = []
                    allowed = []
                    for b in req_ids:
                        if ensure_staff_module_permission(user, 'payments', 'view', building_id=b):
                            allowed.append(b)
                    if not allowed:
                        if not ensure_staff_module_permission(user, 'payments', 'view'):
                            return qs.none()
                    else:
                        qs = qs.filter(invoice__booking__building_id__in=allowed)
                else:
                    perms = getattr(user, 'permissions', {}) or {}
                    allowed_ids = []
                    for key, scope in (perms.items() if isinstance(perms, dict) else []):
                        if key == 'global':
                            continue
                        try:
                            b = int(key)
                        except Exception:
                            continue
                        mod = (scope or {}).get('payments', {}) or {}
                        if isinstance(mod.get('view'), bool) and mod.get('view'):
                            allowed_ids.append(b)
                    if allowed_ids:
                        qs = qs.filter(invoice__booking__building_id__in=allowed_ids)
                    else:
                        if not ensure_staff_module_permission(user, 'payments', 'view'):
                            return qs.none()
            else:
                return qs.none()
        # Apply building filters for all roles (including pg_admin/superuser) after RBAC scoping
        params = self.request.query_params
        building = params.get("building") or params.get("building_id")
        building_in = params.get("building__in") or params.get("building_id__in") or params.get("building_ids")
        if building:
            try:
                b_id = int(building)
                qs = qs.filter(invoice__booking__building_id=b_id)
            except Exception:
                pass
        elif building_in:
            try:
                ids = [int(x) for x in str(building_in).split(',') if str(x).strip()]
                if ids:
                    qs = qs.filter(invoice__booking__building_id__in=ids)
            except Exception:
                pass
        # Apply tenant/booking filters for payment history scoping
        # Frontend passes `tenant` and/or `booking` while listing payments
        try:
            booking_param = params.get("booking") or params.get("booking_id")
            tenant_param = params.get("tenant") or params.get("tenant_id")
            invoice_param = params.get("invoice") or params.get("invoice_id")
            if booking_param:
                qs = qs.filter(invoice__booking_id=int(booking_param))
            if tenant_param:
                qs = qs.filter(invoice__booking__tenant_id=int(tenant_param))
            if invoice_param:
                qs = qs.filter(invoice_id=int(invoice_param))
        except Exception:
            # Ignore bad params and return base queryset per RBAC
            pass
        return qs

    def perform_create(self, serializer):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied('Authentication required')
        if getattr(user, 'is_superuser', False):
            instance = serializer.save(created_by=user, updated_by=user)
            try:
                inv = getattr(instance, 'invoice', None)
                owner = getattr(getattr(inv, 'booking', None), 'building', None)
                owner = getattr(owner, 'owner', None)
                meta = {
                    'type': 'payment',
                    'module': 'payments',
                    'payment': {
                        'id': instance.id,
                        'amount': str(instance.amount),
                        'method': instance.method,
                    },
                    'invoice': {
                        'id': getattr(inv, 'id', None),
                        'status': getattr(inv, 'status', None),
                        'balance_due': str(getattr(inv, 'balance_due', '')) if inv else None,
                        'due_date': getattr(inv, 'due_date', None).isoformat() if getattr(inv, 'due_date', None) else None,
                    },
                    'route': f"/invoices/{getattr(inv, 'id', '')}" if inv else None,
                }
                try:
                    booking = getattr(inv, 'booking', None)
                    meta['booking_id'] = getattr(booking, 'id', None)
                    meta['tenant_id'] = getattr(booking, 'tenant_id', None)
                    meta['building_id'] = getattr(booking, 'building_id', None)
                    meta.setdefault('payment', {})['reference'] = getattr(instance, 'reference', None)
                    cm = getattr(inv, 'cycle_month', None)
                    if cm is not None:
                        try:
                            meta['period_label'] = cm.strftime('%b %Y')
                        except Exception:
                            pass
                    # Include readable names for tenant and building
                    try:
                        tenant_obj = getattr(booking, 'tenant', None)
                        building_obj = getattr(booking, 'building', None)
                        tenant_name = getattr(tenant_obj, 'full_name', None) or getattr(tenant_obj, 'name', None)
                        building_name = getattr(building_obj, 'name', None) or getattr(building_obj, 'title', None)
                        if tenant_name:
                            meta['tenant_name'] = tenant_name
                            meta.setdefault('tenant', {})['name'] = tenant_name
                        if building_name:
                            meta['building_name'] = building_name
                            meta.setdefault('building', {})['name'] = building_name
                    except Exception:
                        pass
                except Exception:
                    pass
                log_activity(user, 'create', description='Payment created', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'create', description='Payment created', meta=meta)
            except Exception:
                pass
            return
        role = getattr(user, 'role', None)
        invoice = serializer.validated_data.get('invoice')
        owner_id = getattr(invoice.booking.building, 'owner_id', None) if invoice else None
        building_id = getattr(invoice.booking, 'building_id', None) if invoice else None
        if role == 'pg_admin':
            if owner_id and owner_id != getattr(user, 'id', None):
                raise PermissionDenied('You cannot create a payment for this invoice.')
            instance = serializer.save(created_by=user, updated_by=user)
            try:
                inv = getattr(instance, 'invoice', None)
                owner = getattr(getattr(inv, 'booking', None), 'building', None)
                owner = getattr(owner, 'owner', None)
                meta = {
                    'type': 'payment',
                    'module': 'payments',
                    'payment': {
                        'id': instance.id,
                        'amount': str(instance.amount),
                        'method': instance.method,
                    },
                    'invoice': {
                        'id': getattr(inv, 'id', None),
                        'status': getattr(inv, 'status', None),
                        'balance_due': str(getattr(inv, 'balance_due', '')) if inv else None,
                        'due_date': getattr(inv, 'due_date', None).isoformat() if getattr(inv, 'due_date', None) else None,
                    },
                    'route': f"/invoices/{getattr(inv, 'id', '')}" if inv else None,
                }
                try:
                    booking = getattr(inv, 'booking', None)
                    meta['booking_id'] = getattr(booking, 'id', None)
                    meta['tenant_id'] = getattr(booking, 'tenant_id', None)
                    meta['building_id'] = getattr(booking, 'building_id', None)
                    meta.setdefault('payment', {})['reference'] = getattr(instance, 'reference', None)
                    cm = getattr(inv, 'cycle_month', None)
                    if cm is not None:
                        try:
                            meta['period_label'] = cm.strftime('%b %Y')
                        except Exception:
                            pass
                    # Include readable names for tenant and building
                    try:
                        tenant_obj = getattr(booking, 'tenant', None)
                        building_obj = getattr(booking, 'building', None)
                        tenant_name = getattr(tenant_obj, 'full_name', None) or getattr(tenant_obj, 'name', None)
                        building_name = getattr(building_obj, 'name', None) or getattr(building_obj, 'title', None)
                        if tenant_name:
                            meta['tenant_name'] = tenant_name
                            meta.setdefault('tenant', {})['name'] = tenant_name
                        if building_name:
                            meta['building_name'] = building_name
                            meta.setdefault('building', {})['name'] = building_name
                    except Exception:
                        pass
                except Exception:
                    pass
                log_activity(user, 'create', description='Payment created', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'create', description='Payment created', meta=meta)
            except Exception:
                pass
            return
        if role == 'pg_staff':
            if owner_id and owner_id != getattr(user, 'pg_admin_id', None):
                raise PermissionDenied('You cannot create a payment for this invoice.')
            if not ensure_staff_module_permission(user, 'payments', 'add', building_id=building_id):
                raise PermissionDenied('You do not have permission to add payments.')
            instance = serializer.save(created_by=user, updated_by=user)
            try:
                inv = getattr(instance, 'invoice', None)
                owner = getattr(getattr(inv, 'booking', None), 'building', None)
                owner = getattr(owner, 'owner', None)
                meta = {
                    'type': 'payment',
                    'module': 'payments',
                    'payment': {
                        'id': instance.id,
                        'amount': str(instance.amount),
                        'method': instance.method,
                    },
                    'invoice': {
                        'id': getattr(inv, 'id', None),
                        'status': getattr(inv, 'status', None),
                        'balance_due': str(getattr(inv, 'balance_due', '')) if inv else None,
                        'due_date': getattr(inv, 'due_date', None).isoformat() if getattr(inv, 'due_date', None) else None,
                    },
                    'route': f"/invoices/{getattr(inv, 'id', '')}" if inv else None,
                }
                try:
                    booking = getattr(inv, 'booking', None)
                    meta['booking_id'] = getattr(booking, 'id', None)
                    meta['tenant_id'] = getattr(booking, 'tenant_id', None)
                    meta['building_id'] = getattr(booking, 'building_id', None)
                    meta.setdefault('payment', {})['reference'] = getattr(instance, 'reference', None)
                    cm = getattr(inv, 'cycle_month', None)
                    if cm is not None:
                        try:
                            meta['period_label'] = cm.strftime('%b %Y')
                        except Exception:
                            pass
                    # Include readable names for tenant and building
                    try:
                        tenant_obj = getattr(booking, 'tenant', None)
                        building_obj = getattr(booking, 'building', None)
                        tenant_name = getattr(tenant_obj, 'full_name', None) or getattr(tenant_obj, 'name', None)
                        building_name = getattr(building_obj, 'name', None) or getattr(building_obj, 'title', None)
                        if tenant_name:
                            meta['tenant_name'] = tenant_name
                            meta.setdefault('tenant', {})['name'] = tenant_name
                        if building_name:
                            meta['building_name'] = building_name
                            meta.setdefault('building', {})['name'] = building_name
                    except Exception:
                        pass
                except Exception:
                    pass
                log_activity(user, 'create', description='Payment created', meta=meta)
                if owner:
                    log_activity(owner, 'create', description='Payment created', meta=meta)
            except Exception:
                pass
            return
        raise PermissionDenied('You are not allowed to create payments.')

    def perform_update(self, serializer):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied('Authentication required')
        if getattr(user, 'is_superuser', False):
            instance = serializer.save(updated_by=user)
            try:
                inv = getattr(instance, 'invoice', None)
                owner = getattr(getattr(inv, 'booking', None), 'building', None)
                owner = getattr(owner, 'owner', None)
                meta = {
                    'type': 'payment',
                    'module': 'payments',
                    'payment': {
                        'id': instance.id,
                        'amount': str(instance.amount),
                        'method': instance.method,
                    },
                    'invoice': {
                        'id': getattr(inv, 'id', None),
                        'status': getattr(inv, 'status', None),
                        'balance_due': str(getattr(inv, 'balance_due', '')) if inv else None,
                        'due_date': getattr(inv, 'due_date', None).isoformat() if getattr(inv, 'due_date', None) else None,
                    },
                    'route': f"/invoices/{getattr(inv, 'id', '')}" if inv else None,
                }
                try:
                    booking = getattr(inv, 'booking', None)
                    meta['booking_id'] = getattr(booking, 'id', None)
                    meta['tenant_id'] = getattr(booking, 'tenant_id', None)
                    meta['building_id'] = getattr(booking, 'building_id', None)
                    meta.setdefault('payment', {})['reference'] = getattr(instance, 'reference', None)
                    cm = getattr(inv, 'cycle_month', None)
                    if cm is not None:
                        try:
                            meta['period_label'] = cm.strftime('%b %Y')
                        except Exception:
                            pass
                    # Include readable names for tenant and building
                    try:
                        tenant_obj = getattr(booking, 'tenant', None)
                        building_obj = getattr(booking, 'building', None)
                        tenant_name = getattr(tenant_obj, 'full_name', None) or getattr(tenant_obj, 'name', None)
                        building_name = getattr(building_obj, 'name', None) or getattr(building_obj, 'title', None)
                        if tenant_name:
                            meta['tenant_name'] = tenant_name
                            meta.setdefault('tenant', {})['name'] = tenant_name
                        if building_name:
                            meta['building_name'] = building_name
                            meta.setdefault('building', {})['name'] = building_name
                    except Exception:
                        pass
                except Exception:
                    pass
                log_activity(user, 'update', description='Payment updated', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'update', description='Payment updated', meta=meta)
            except Exception:
                pass
            return
        role = getattr(user, 'role', None)
        instance: Payment = self.get_object()
        # Determine target invoice/building after potential change
        target_invoice = serializer.validated_data.get('invoice', instance.invoice)
        owner_id = getattr(target_invoice.booking.building, 'owner_id', None) if target_invoice else None
        building_id = getattr(target_invoice.booking, 'building_id', None) if target_invoice else None
        if role == 'pg_admin':
            if owner_id and owner_id != getattr(user, 'id', None):
                raise PermissionDenied('You cannot modify this payment.')
            instance = serializer.save(updated_by=user)
            try:
                inv = getattr(instance, 'invoice', None)
                owner = getattr(getattr(inv, 'booking', None), 'building', None)
                owner = getattr(owner, 'owner', None)
                meta = {
                    'type': 'payment',
                    'module': 'payments',
                    'payment': {
                        'id': instance.id,
                        'amount': str(instance.amount),
                        'method': instance.method,
                    },
                    'invoice': {
                        'id': getattr(inv, 'id', None),
                        'status': getattr(inv, 'status', None),
                        'balance_due': str(getattr(inv, 'balance_due', '')) if inv else None,
                        'due_date': getattr(inv, 'due_date', None).isoformat() if getattr(inv, 'due_date', None) else None,
                    },
                    'route': f"/invoices/{getattr(inv, 'id', '')}" if inv else None,
                }
                try:
                    booking = getattr(inv, 'booking', None)
                    meta['booking_id'] = getattr(booking, 'id', None)
                    meta['tenant_id'] = getattr(booking, 'tenant_id', None)
                    meta['building_id'] = getattr(booking, 'building_id', None)
                    meta.setdefault('payment', {})['reference'] = getattr(instance, 'reference', None)
                    cm = getattr(inv, 'cycle_month', None)
                    if cm is not None:
                        try:
                            meta['period_label'] = cm.strftime('%b %Y')
                        except Exception:
                            pass
                    # Include readable names for tenant and building
                    try:
                        tenant_obj = getattr(booking, 'tenant', None)
                        building_obj = getattr(booking, 'building', None)
                        tenant_name = getattr(tenant_obj, 'full_name', None) or getattr(tenant_obj, 'name', None)
                        building_name = getattr(building_obj, 'name', None) or getattr(building_obj, 'title', None)
                        if tenant_name:
                            meta['tenant_name'] = tenant_name
                            meta.setdefault('tenant', {})['name'] = tenant_name
                        if building_name:
                            meta['building_name'] = building_name
                            meta.setdefault('building', {})['name'] = building_name
                    except Exception:
                        pass
                except Exception:
                    pass
                log_activity(user, 'update', description='Payment updated', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'update', description='Payment updated', meta=meta)
            except Exception:
                pass
            return
        if role == 'pg_staff':
            if owner_id and owner_id != getattr(user, 'pg_admin_id', None):
                raise PermissionDenied('You cannot modify this payment.')
            if not ensure_staff_module_permission(user, 'payments', 'edit', building_id=building_id):
                raise PermissionDenied('You do not have permission to edit payments.')
            instance = serializer.save(updated_by=user)
            try:
                inv = getattr(instance, 'invoice', None)
                owner = getattr(getattr(inv, 'booking', None), 'building', None)
                owner = getattr(owner, 'owner', None)
                meta = {
                    'type': 'payment',
                    'module': 'payments',
                    'payment': {
                        'id': instance.id,
                        'amount': str(instance.amount),
                        'method': instance.method,
                    },
                    'invoice': {
                        'id': getattr(inv, 'id', None),
                        'status': getattr(inv, 'status', None),
                        'balance_due': str(getattr(inv, 'balance_due', '')) if inv else None,
                        'due_date': getattr(inv, 'due_date', None).isoformat() if getattr(inv, 'due_date', None) else None,
                    },
                    'route': f"/invoices/{getattr(inv, 'id', '')}" if inv else None,
                }
                try:
                    booking = getattr(inv, 'booking', None)
                    meta['booking_id'] = getattr(booking, 'id', None)
                    meta['tenant_id'] = getattr(booking, 'tenant_id', None)
                    meta['building_id'] = getattr(booking, 'building_id', None)
                    meta.setdefault('payment', {})['reference'] = getattr(instance, 'reference', None)
                    cm = getattr(inv, 'cycle_month', None)
                    if cm is not None:
                        try:
                            meta['period_label'] = cm.strftime('%b %Y')
                        except Exception:
                            pass
                    # Include readable names for tenant and building
                    try:
                        tenant_obj = getattr(booking, 'tenant', None)
                        building_obj = getattr(booking, 'building', None)
                        tenant_name = getattr(tenant_obj, 'full_name', None) or getattr(tenant_obj, 'name', None)
                        building_name = getattr(building_obj, 'name', None) or getattr(building_obj, 'title', None)
                        if tenant_name:
                            meta['tenant_name'] = tenant_name
                            meta.setdefault('tenant', {})['name'] = tenant_name
                        if building_name:
                            meta['building_name'] = building_name
                            meta.setdefault('building', {})['name'] = building_name
                    except Exception:
                        pass
                except Exception:
                    pass
                log_activity(user, 'update', description='Payment updated', meta=meta)
                if owner:
                    log_activity(owner, 'update', description='Payment updated', meta=meta)
            except Exception:
                pass
            return
        raise PermissionDenied('You cannot modify this payment.')

class ExpenseViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = Expense.objects.select_related("building", "created_by", "updated_by").all()
    serializer_class = ExpenseSerializer
    ordering = ("-expense_date", "-created_at")

    def get_queryset(self):
        qs = super().get_queryset()
        # Ownership isolation via building.owner
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not getattr(user, "is_superuser", False):
            role = getattr(user, "role", None)
            if role == "pg_admin":
                qs = qs.filter(building__owner=user)
            elif role == "pg_staff" and getattr(user, "pg_admin_id", None):
                qs = qs.filter(building__owner_id=user.pg_admin_id)
                # Respect building-scoped 'view' permission
                params = self.request.query_params
                building = params.get("building")
                building_in = params.get("building__in")
                if building:
                    try:
                        b_id = int(building)
                    except Exception:
                        return qs.none()
                    if not ensure_staff_module_permission(user, 'expenses', 'view', building_id=b_id):
                        if not ensure_staff_module_permission(user, 'expenses', 'view'):
                            return qs.none()
                    qs = qs.filter(building_id=b_id)
                elif building_in:
                    try:
                        req_ids = [int(x) for x in str(building_in).split(',') if str(x).strip()]
                    except Exception:
                        req_ids = []
                    allowed = []
                    for b in req_ids:
                        if ensure_staff_module_permission(user, 'expenses', 'view', building_id=b):
                            allowed.append(b)
                    if not allowed:
                        if not ensure_staff_module_permission(user, 'expenses', 'view'):
                            return qs.none()
                    else:
                        qs = qs.filter(building_id__in=allowed)
                else:
                    perms = getattr(user, 'permissions', {}) or {}
                    allowed_ids = []
                    for key, scope in (perms.items() if isinstance(perms, dict) else []):
                        if key == 'global':
                            continue
                        try:
                            b = int(key)
                        except Exception:
                            continue
                        mod = (scope or {}).get('expenses', {}) or {}
                        if isinstance(mod.get('view'), bool) and mod.get('view'):
                            allowed_ids.append(b)
                    if allowed_ids:
                        qs = qs.filter(building_id__in=allowed_ids)
                    else:
                        if not ensure_staff_module_permission(user, 'expenses', 'view'):
                            return qs.none()
            else:
                return qs.none()
        return qs

    def perform_create(self, serializer):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied('Authentication required')
        if getattr(user, 'is_superuser', False):
            instance = serializer.save(created_by=user, updated_by=user)
            try:
                owner = getattr(getattr(instance, 'building', None), 'owner', None)
                meta = {
                    'type': 'expense',
                    'module': 'expenses',
                    'expense': {
                        'id': getattr(instance, 'id', None),
                        'category': getattr(instance, 'category', None),
                        'amount': str(getattr(instance, 'amount', '')),
                        'expense_date': getattr(instance, 'expense_date', None).isoformat() if getattr(instance, 'expense_date', None) else None,
                    },
                    'building_id': getattr(instance, 'building_id', None),
                    'route': f"/expenses/{getattr(instance, 'id', '')}",
                }
                log_activity(user, 'create', description='Expense created', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'create', description='Expense created', meta=meta)
            except Exception:
                pass
            return
        role = getattr(user, 'role', None)
        building = serializer.validated_data.get('building')
        owner_id = getattr(building, 'owner_id', None) if building else None
        building_id = getattr(building, 'id', None) if building else None
        if role == 'pg_admin':
            if owner_id and owner_id != getattr(user, 'id', None):
                raise PermissionDenied('You cannot create an expense for this building.')
            instance = serializer.save(created_by=user, updated_by=user)
            try:
                owner = getattr(getattr(instance, 'building', None), 'owner', None)
                meta = {
                    'type': 'expense',
                    'module': 'expenses',
                    'expense': {
                        'id': getattr(instance, 'id', None),
                        'category': getattr(instance, 'category', None),
                        'amount': str(getattr(instance, 'amount', '')),
                        'expense_date': getattr(instance, 'expense_date', None).isoformat() if getattr(instance, 'expense_date', None) else None,
                    },
                    'building_id': getattr(instance, 'building_id', None),
                    'route': f"/expenses/{getattr(instance, 'id', '')}",
                }
                log_activity(user, 'create', description='Expense created', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'create', description='Expense created', meta=meta)
            except Exception:
                pass
            return
        if role == 'pg_staff':
            if owner_id and owner_id != getattr(user, 'pg_admin_id', None):
                raise PermissionDenied('You cannot create an expense for this building.')
            if not ensure_staff_module_permission(user, 'expenses', 'add', building_id=building_id):
                raise PermissionDenied('You do not have permission to add expenses.')
            instance = serializer.save(created_by=user, updated_by=user)
            try:
                owner = getattr(getattr(instance, 'building', None), 'owner', None)
                meta = {
                    'type': 'expense',
                    'module': 'expenses',
                    'expense': {
                        'id': getattr(instance, 'id', None),
                        'category': getattr(instance, 'category', None),
                        'amount': str(getattr(instance, 'amount', '')),
                        'expense_date': getattr(instance, 'expense_date', None).isoformat() if getattr(instance, 'expense_date', None) else None,
                    },
                    'building_id': getattr(instance, 'building_id', None),
                    'route': f"/expenses/{getattr(instance, 'id', '')}",
                }
                # Log to staff actor and the admin owner so both see it
                log_activity(user, 'create', description='Expense created', meta=meta)
                if owner:
                    log_activity(owner, 'create', description='Expense created', meta=meta)
            except Exception:
                pass
            return
        raise PermissionDenied('You are not allowed to create expenses.')

    def perform_update(self, serializer):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied('Authentication required')
        if getattr(user, 'is_superuser', False):
            instance = serializer.save(updated_by=user)
            try:
                owner = getattr(getattr(instance, 'building', None), 'owner', None)
                meta = {
                    'type': 'expense',
                    'module': 'expenses',
                    'expense': {
                        'id': getattr(instance, 'id', None),
                        'category': getattr(instance, 'category', None),
                        'amount': str(getattr(instance, 'amount', '')),
                        'expense_date': getattr(instance, 'expense_date', None).isoformat() if getattr(instance, 'expense_date', None) else None,
                    },
                    'building_id': getattr(instance, 'building_id', None),
                    'route': f"/expenses/{getattr(instance, 'id', '')}",
                }
                log_activity(user, 'update', description='Expense updated', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'update', description='Expense updated', meta=meta)
            except Exception:
                pass
            return
        role = getattr(user, 'role', None)
        instance: Expense = self.get_object()
        target_building = serializer.validated_data.get('building', instance.building)
        owner_id = getattr(target_building, 'owner_id', None) if target_building else None
        building_id = getattr(target_building, 'id', None) if target_building else None
        if role == 'pg_admin':
            if owner_id and owner_id != getattr(user, 'id', None):
                raise PermissionDenied('You cannot modify this expense.')
            instance = serializer.save(updated_by=user)
            try:
                owner = getattr(getattr(instance, 'building', None), 'owner', None)
                meta = {
                    'type': 'expense',
                    'module': 'expenses',
                    'expense': {
                        'id': getattr(instance, 'id', None),
                        'category': getattr(instance, 'category', None),
                        'amount': str(getattr(instance, 'amount', '')),
                        'expense_date': getattr(instance, 'expense_date', None).isoformat() if getattr(instance, 'expense_date', None) else None,
                    },
                    'building_id': getattr(instance, 'building_id', None),
                    'route': f"/expenses/{getattr(instance, 'id', '')}",
                }
                log_activity(user, 'update', description='Expense updated', meta=meta)
                if owner and getattr(owner, 'id', None) != getattr(user, 'id', None):
                    log_activity(owner, 'update', description='Expense updated', meta=meta)
            except Exception:
                pass
            return
        if role == 'pg_staff':
            if owner_id and owner_id != getattr(user, 'pg_admin_id', None):
                raise PermissionDenied('You cannot modify this expense.')
            if not ensure_staff_module_permission(user, 'expenses', 'edit', building_id=building_id):
                raise PermissionDenied('You do not have permission to edit expenses.')
            instance = serializer.save(updated_by=user)
            try:
                owner = getattr(getattr(instance, 'building', None), 'owner', None)
                meta = {
                    'type': 'expense',
                    'module': 'expenses',
                    'expense': {
                        'id': getattr(instance, 'id', None),
                        'category': getattr(instance, 'category', None),
                        'amount': str(getattr(instance, 'amount', '')),
                        'expense_date': getattr(instance, 'expense_date', None).isoformat() if getattr(instance, 'expense_date', None) else None,
                    },
                    'building_id': getattr(instance, 'building_id', None),
                    'route': f"/expenses/{getattr(instance, 'id', '')}",
                }
                # Log to staff actor and the admin owner so both see it
                log_activity(user, 'update', description='Expense updated', meta=meta)
                if owner:
                    log_activity(owner, 'update', description='Expense updated', meta=meta)
            except Exception:
                pass
            return
        raise PermissionDenied('You cannot modify this expense.')

    def perform_destroy(self, instance):
        user = getattr(self.request, 'user', None)
        if not user or not user.is_authenticated:
            raise PermissionDenied('Authentication required')
        if getattr(user, 'is_superuser', False):
            instance.delete()
            return
        role = getattr(user, 'role', None)
        owner_id = getattr(getattr(instance, 'building', None), 'owner_id', None)
        building_id = getattr(instance, 'building_id', None)
        if role == 'pg_admin':
            if owner_id and owner_id != getattr(user, 'id', None):
                raise PermissionDenied('You cannot delete this expense.')
            instance.delete()
            return
        if role == 'pg_staff':
            if owner_id and owner_id != getattr(user, 'pg_admin_id', None):
                raise PermissionDenied('You cannot delete this expense.')
            if ensure_staff_module_permission(user, 'expenses', 'delete', building_id=building_id):
                instance.delete()
                return
            raise PermissionDenied('You do not have permission to delete expenses.')
        raise PermissionDenied('You cannot delete this expense.')


class ExpenseCategoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = ExpenseCategory.objects.all()
    serializer_class = ExpenseCategorySerializer
    ordering = ("name",)

    def get_queryset(self):
        qs = super().get_queryset()
        # Ownership isolation via owner (pg_admin)
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return qs.none()
        params = self.request.query_params
        if getattr(user, "is_superuser", False):
            owner = params.get("owner")
            if owner:
                qs = qs.filter(owner_id=owner)
            return qs
        else:
            role = getattr(user, "role", None)
            if role == "pg_admin":
                # Ensure defaults exist for this admin (idempotent)
                try:
                    from .models import ExpenseCategory
                    for key in DEFAULT_EXPENSE_CATEGORIES:
                        name = key.replace('_', ' ').title()
                        ExpenseCategory.objects.get_or_create(owner_id=user.id, name=name, defaults={"is_active": True})
                except Exception:
                    pass
                # Only own categories
                qs = qs.filter(owner_id=user.id)
            elif role == "pg_staff" and getattr(user, "pg_admin_id", None):
                # Ensure defaults exist for their admin (idempotent)
                try:
                    from .models import ExpenseCategory
                    for key in DEFAULT_EXPENSE_CATEGORIES:
                        name = key.replace('_', ' ').title()
                        ExpenseCategory.objects.get_or_create(owner_id=user.pg_admin_id, name=name, defaults={"is_active": True})
                except Exception:
                    pass
                # Only their admin's categories
                qs = qs.filter(owner_id=user.pg_admin_id)
            else:
                return qs.none()

        # Existing active/is_active filtering
        active = params.get("active")
        is_active = params.get("is_active")

        def to_bool(val):
            if val is None:
                return None
            if val in {"1", "true", "True", "yes", "on"}:
                return True
            if val in {"0", "false", "False", "no", "off"}:
                return False
            return None

        flag = to_bool(is_active)
        if flag is None:
            flag = to_bool(active)

        if flag is True:
            qs = qs.filter(is_active=True)
        elif flag is False:
            qs = qs.filter(is_active=False)
        return qs

    def perform_create(self, serializer):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")
        # pg_staff cannot create categories
        if not getattr(user, "is_superuser", False) and getattr(user, "role", None) == "pg_staff":
            raise PermissionDenied("Staff cannot create expense categories")
        # Set owner for pg_admin; for superuser, allow owner from payload or leave null
        owner_id = None
        try:
            owner_id = int(self.request.data.get("owner") or 0)
        except Exception:
            owner_id = None
        if getattr(user, "is_superuser", False):
            if owner_id:
                serializer.save(owner_id=owner_id)
            else:
                serializer.save()
        else:
            # pg_admin creating => force owner=self
            serializer.save(owner=user)

    def perform_update(self, serializer):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")
        # pg_staff cannot update categories
        if not getattr(user, "is_superuser", False) and getattr(user, "role", None) == "pg_staff":
            raise PermissionDenied("Staff cannot update expense categories")
        if getattr(user, "is_superuser", False):
            # Superuser may change anything (including owner via payload if bound at serializer)
            serializer.save()
        else:
            # pg_admin cannot reassign owner; ensure owner remains self
            serializer.save(owner=user)

    def perform_destroy(self, instance):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")
        if not getattr(user, "is_superuser", False) and getattr(user, "role", None) == "pg_staff":
            raise PermissionDenied("Staff cannot delete expense categories")
        # Allow pg_admin and superuser to delete (including global)
        instance.delete()


class InvoiceExpenseViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = InvoiceExpense.objects.select_related("invoice").all()
    serializer_class = InvoiceExpenseSerializer
    ordering = ("invoice", "-created_at")

    def get_queryset(self):
        qs = super().get_queryset()
        # Ownership isolation via invoice.booking.building.owner
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not getattr(user, "is_superuser", False):
            role = getattr(user, "role", None)
            if role == "pg_admin":
                qs = qs.filter(invoice__booking__building__owner=user)
            elif role == "pg_staff" and getattr(user, "pg_admin_id", None):
                qs = qs.filter(invoice__booking__building__owner_id=user.pg_admin_id)
            else:
                return qs.none()
        return qs


class InvoiceSettingsViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    queryset = InvoiceSettings.objects.select_related("owner", "building").all()
    serializer_class = InvoiceSettingsSerializer
    ordering = ("owner_id", "building_id")

    def get_queryset(self):
        qs = super().get_queryset()
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            return qs.none()
        params = self.request.query_params
        if getattr(user, "is_superuser", False):
            owner = params.get("owner")
            building = params.get("building")
            if owner:
                qs = qs.filter(owner_id=owner)
            if building:
                qs = qs.filter(building_id=building)
            return qs
        role = getattr(user, "role", None)
        if role == "pg_admin":
            qs = qs.filter(owner_id=getattr(user, "id", None))
            building = params.get("building")
            if building:
                qs = qs.filter(building_id=building)
            return qs
        if role == "pg_staff" and getattr(user, "pg_admin_id", None):
            qs = qs.filter(owner_id=user.pg_admin_id)
            building = params.get("building")
            if building:
                qs = qs.filter(building_id=building)
            return qs
        return qs.none()

    def perform_create(self, serializer):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")
        if getattr(user, "is_superuser", False):
            serializer.save()
            return
        role = getattr(user, "role", None)
        if role == "pg_admin":
            serializer.save(owner=user)
            return
        # Staff cannot create settings
        raise PermissionDenied("You are not allowed to create invoice settings.")

    def perform_update(self, serializer):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")
        if getattr(user, "is_superuser", False):
            serializer.save()
            return
        role = getattr(user, "role", None)
        instance: InvoiceSettings = self.get_object()
        if role == "pg_admin" and getattr(instance, "owner_id", None) == getattr(user, "id", None):
            # Enforce owner remains the same
            serializer.save(owner=user)
            return
        raise PermissionDenied("You cannot modify these invoice settings.")

    def perform_destroy(self, instance):
        user = getattr(self.request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")
        if getattr(user, "is_superuser", False):
            instance.delete()
            return
        role = getattr(user, "role", None)
        if role == "pg_admin" and getattr(instance, "owner_id", None) == getattr(user, "id", None):
            instance.delete()
            return
        raise PermissionDenied("You cannot delete these invoice settings.")

    @action(detail=False, methods=["get"], url_path="current")
    def current(self, request):
        user = request.user
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")
        building_id = request.query_params.get("building")
        # Resolve owner scope
        owner_id = getattr(user, "id", None) if getattr(user, "role", None) == "pg_admin" or getattr(user, "is_superuser", False) else getattr(user, "pg_admin_id", None)
        if not owner_id:
            return Response({"detail": "Unsupported user role for settings."}, status=status.HTTP_403_FORBIDDEN)
        # Prefer building-specific; fall back to global
        inst = None
        if building_id:
            try:
                inst = InvoiceSettings.objects.filter(owner_id=owner_id, building_id=int(building_id)).first()
            except Exception:
                return Response({"detail": "building must be an integer id"}, status=status.HTTP_400_BAD_REQUEST)
        if not inst:
            inst = InvoiceSettings.objects.filter(owner_id=owner_id, building__isnull=True).first()
        # If not found and user can create, provision defaults
        if not inst and (getattr(user, "is_superuser", False) or getattr(user, "role", None) == "pg_admin"):
            defaults = dict(
                generate_type=InvoiceSettings.GenerateType.AUTOMATIC,
                period=InvoiceSettings.Period.MONTHLY,
                monthly_cycle=InvoiceSettings.MonthlyCycle.CHECKIN_DATE,
                weekly_cycle=InvoiceSettings.WeeklyCycle.CHECKIN_DATE,
                generate_on=InvoiceSettings.GenerateOn.START,
            )
            try:
                inst = InvoiceSettings.objects.create(owner_id=owner_id, building_id=int(building_id) if building_id else None, **defaults)
            except Exception:
                # If invalid building id or race, try global create
                inst, _ = InvoiceSettings.objects.get_or_create(owner_id=owner_id, building=None, defaults=defaults)
        if not inst:
            # Staff may not auto-create; return 404 if nothing to show
            return Response({"detail": "Settings not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(InvoiceSettingsSerializer(inst).data)


class CashflowView(APIView):
    permission_classes = [IsAuthenticated]

    def get_date_range(self, request):
        preset = (request.query_params.get("preset") or "").strip().lower()
        start = request.query_params.get("start")
        end = request.query_params.get("end")
        today = timezone.localdate()
        # Presets
        if preset == "today":
            return today, today
        if preset == "yesterday":
            y = today - timedelta(days=1)
            return y, y
        if preset == "last7":
            return today - timedelta(days=6), today
        if preset == "last15":
            return today - timedelta(days=14), today
        if preset == "last30":
            return today - timedelta(days=29), today
        if preset == "this_month":
            first = today.replace(day=1)
            return first, today
        if preset == "last_month":
            first_this = today.replace(day=1)
            last_month_end = first_this - timedelta(days=1)
            last_month_start = last_month_end.replace(day=1)
            return last_month_start, last_month_end
        if preset == "this_year":
            first = today.replace(month=1, day=1)
            return first, today
        if preset == "last_year":
            first_this = today.replace(month=1, day=1)
            last_year_end = first_this - timedelta(days=1)
            last_year_start = last_year_end.replace(month=1, day=1)
            return last_year_start, last_year_end
        # Custom range
        if start and end:
            d_from = parse_date(start)
            d_to = parse_date(end)
            if d_from and d_to:
                return d_from, d_to
        # default fallback
        return today - timedelta(days=29), today

    def get(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            raise PermissionDenied("Authentication required")

        d_from, d_to = self.get_date_range(request)
        building_id = request.query_params.get("building_id") or request.query_params.get("building")

        # Base querysets with ownership scoping similar to PaymentViewSet / ExpenseViewSet
        pay_qs = Payment.objects.select_related("invoice", "invoice__booking", "invoice__booking__building").all()
        exp_qs = Expense.objects.select_related("building").all()

        if not getattr(user, "is_superuser", False):
            role = getattr(user, "role", None)
            if role == "pg_admin":
                pay_qs = pay_qs.filter(invoice__booking__building__owner=user)
                exp_qs = exp_qs.filter(building__owner=user)
            elif role == "pg_staff" and getattr(user, "pg_admin_id", None):
                pay_qs = pay_qs.filter(invoice__booking__building__owner_id=user.pg_admin_id)
                exp_qs = exp_qs.filter(building__owner_id=user.pg_admin_id)
                # enforce building scoped view if a building is provided
                if building_id:
                    try:
                        b_id = int(building_id)
                    except Exception:
                        return Response({"detail": "Invalid building_id"}, status=status.HTTP_400_BAD_REQUEST)
                    if not ensure_staff_module_permission(user, 'payments', 'view', building_id=b_id) and not ensure_staff_module_permission(user, 'expenses', 'view', building_id=b_id):
                        raise PermissionDenied("You do not have permission to view this building's data")
                    pay_qs = pay_qs.filter(invoice__booking__building_id=b_id)
                    exp_qs = exp_qs.filter(building_id=b_id)
                else:
                    # If no building passed, restrict to buildings where staff has 'view' for either payments or expenses
                    perms = getattr(user, 'permissions', {}) or {}
                    allowed_ids = []
                    for key, scope in (perms.items() if isinstance(perms, dict) else []):
                        if key == 'global':
                            continue
                        try:
                            b = int(key)
                        except Exception:
                            continue
                        p = (scope or {}).get('payments', {}) or {}
                        e = (scope or {}).get('expenses', {}) or {}
                        if (isinstance(p.get('view'), bool) and p.get('view')) or (isinstance(e.get('view'), bool) and e.get('view')):
                            allowed_ids.append(b)
                    if allowed_ids:
                        pay_qs = pay_qs.filter(invoice__booking__building_id__in=allowed_ids)
                        exp_qs = exp_qs.filter(building_id__in=allowed_ids)
            else:
                pay_qs = pay_qs.none()
                exp_qs = exp_qs.none()

        # Date filters
        pay_qs = pay_qs.filter(received_at__date__gte=d_from, received_at__date__lte=d_to)
        exp_qs = exp_qs.filter(expense_date__gte=d_from, expense_date__lte=d_to)
        if building_id and (getattr(user, 'is_superuser', False) or getattr(user, 'role', None) == 'pg_admin'):
            try:
                b_id = int(building_id)
                pay_qs = pay_qs.filter(invoice__booking__building_id=b_id)
                exp_qs = exp_qs.filter(building_id=b_id)
            except Exception:
                pass

        # Aggregations
        # Monthly (by calendar month within range)
        from collections import defaultdict
        import calendar

        try:
            monthly_map = defaultdict(lambda: {"income": 0, "expenses": 0})
            # Sum payments by month (DB-side grouping to avoid datetime pitfalls)
            pay_rows = (
                pay_qs
                .annotate(y=ExtractYear("received_at"), m=ExtractMonth("received_at"))
                .values("y", "m")
                .annotate(total=Sum("amount"))
            )
            for r in pay_rows:
                key = (r["y"], r["m"])
                monthly_map[key]["income"] += float(r["total"]) if r["total"] is not None else 0

            # Sum expenses by month (DB-side grouping)
            exp_rows = (
                exp_qs
                .annotate(y=ExtractYear("expense_date"), m=ExtractMonth("expense_date"))
                .values("y", "m")
                .annotate(total=Sum("amount"))
            )
            for r in exp_rows:
                key = (r["y"], r["m"])
                monthly_map[key]["expenses"] += float(r["total"]) if r["total"] is not None else 0
            # Build ordered list across the span
            months = []
            cur = d_from.replace(day=1)
            end_marker = d_to.replace(day=1)
            while cur <= end_marker:
                key = (cur.year, cur.month)
                income = monthly_map[key]["income"]
                expenses = monthly_map[key]["expenses"]
                months.append({
                    "month": calendar.month_abbr[cur.month],
                    "income": round(income, 2),
                    "expenses": round(expenses, 2),
                    "net": round(income - expenses, 2),
                })
                # advance one month
                if cur.month == 12:
                    cur = cur.replace(year=cur.year + 1, month=1)
                else:
                    cur = cur.replace(month=cur.month + 1)
        except Exception as e:
            logger.exception("Cashflow monthly aggregation failed", extra={
                "d_from": d_from.isoformat() if d_from else None,
                "d_to": d_to.isoformat() if d_to else None,
                "building_id": building_id,
                "user_id": getattr(user, 'id', None),
            })
            return Response({"detail": "Cashflow aggregation error (monthly)", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Categories aggregation
        try:
            cat_rows = (
                exp_qs.values("category")
                .annotate(total=Sum("amount"))
                .order_by("category")
            )
            categories = [
                {"name": r["category"] or "Uncategorized", "value": float(r["total"]) if r["total"] is not None else 0}
                for r in cat_rows
            ]
        except Exception as e:
            logger.exception("Cashflow categories aggregation failed", extra={
                "d_from": d_from.isoformat() if d_from else None,
                "d_to": d_to.isoformat() if d_to else None,
                "building_id": building_id,
                "user_id": getattr(user, 'id', None),
            })
            return Response({"detail": "Cashflow aggregation error (categories)", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Daily expenses
        try:
            daily_rows = (
                exp_qs.values("expense_date")
                .annotate(total=Sum("amount"))
                .order_by("expense_date")
            )
            daily = [
                {"date": r["expense_date"].isoformat(), "amount": float(r["total"]) if r["total"] is not None else 0}
                for r in daily_rows
            ]
        except Exception as e:
            logger.exception("Cashflow daily aggregation failed", extra={
                "d_from": d_from.isoformat() if d_from else None,
                "d_to": d_to.isoformat() if d_to else None,
                "building_id": building_id,
                "user_id": getattr(user, 'id', None),
            })
            return Response({"detail": "Cashflow aggregation error (daily)", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Income vs Expenses by ISO Weekday (1=Mon .. 7=Sun)
        try:
            # Payments by weekday
            pay_wd_rows = (
                pay_qs
                .annotate(w=ExtractIsoWeekDay("received_at"))
                .values("w")
                .annotate(total=Sum("amount"))
            )
            income_by_w = {int(r["w"]): float(r["total"]) if r["total"] is not None else 0.0 for r in pay_wd_rows}

            # Expenses by weekday
            exp_wd_rows = (
                exp_qs
                .annotate(w=ExtractIsoWeekDay("expense_date"))
                .values("w")
                .annotate(total=Sum("amount"))
            )
            expenses_by_w = {int(r["w"]): float(r["total"]) if r["total"] is not None else 0.0 for r in exp_wd_rows}

            # Build ordered Mon..Sun list
            weekday_names = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
            daily_by_weekday = [
                {
                    "weekday": weekday_names[i],
                    "income": round(income_by_w.get(i, 0.0), 2),
                    "expenses": round(expenses_by_w.get(i, 0.0), 2),
                }
                for i in range(1, 8)
            ]
        except Exception as e:
            logger.exception("Cashflow weekday aggregation failed", extra={
                "d_from": d_from.isoformat() if d_from else None,
                "d_to": d_to.isoformat() if d_to else None,
                "building_id": building_id,
                "user_id": getattr(user, 'id', None),
            })
            return Response({"detail": "Cashflow aggregation error (weekday)", "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            "range": {"start": d_from.isoformat(), "end": d_to.isoformat()},
            "monthly": months,
            "categories": categories,
            "daily": daily,
            "daily_by_weekday": daily_by_weekday,
        })
