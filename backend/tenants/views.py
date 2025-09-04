from django.shortcuts import render
from rest_framework import viewsets, permissions
from rest_framework.exceptions import ValidationError
from django.db.models import Prefetch, Q
import logging
from .models import (
    Tenant, EmergencyContact, Stay, TenantBedHistory,
)
from .serializers import (
    TenantSerializer,
    EmergencyContactSerializer,
    StaySerializer,
    BedHistorySerializer,
)
from bookings.models import Booking
from payment.models import Invoice
from subscription.utils import ensure_limit_not_exceeded, get_owner, get_limit, ensure_feature

logger = logging.getLogger(__name__)

class TenantViewSet(viewsets.ModelViewSet):
    queryset = Tenant.objects.all().order_by('full_name')
    serializer_class = TenantSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset().select_related('building')
        # Ownership isolation: superuser sees all, pg_admin sees own data, pg_staff sees their admin's data
        user = getattr(self.request, 'user', None)
        logger.debug(
            "TenantViewSet.get_queryset user=%s role=%s pg_admin_id=%s is_superuser=%s",
            getattr(user, 'id', None), getattr(user, 'role', None), getattr(user, 'pg_admin_id', None), getattr(user, 'is_superuser', False)
        )
        if not user or not user.is_authenticated:
            return qs.none()
        if not user.is_superuser:
            if getattr(user, 'role', None) == 'pg_admin':
                qs = qs.filter(
                    Q(building__owner=user)
                    | Q(stays__bed__room__floor__building__owner=user)
                )
            elif getattr(user, 'role', None) == 'pg_staff' and getattr(user, 'pg_admin_id', None):
                qs = qs.filter(
                    Q(building__owner_id=user.pg_admin_id)
                    | Q(stays__bed__room__floor__building__owner_id=user.pg_admin_id)
                )
            else:
                return qs.none()
        # Prefetch stays with full building chain for efficient access
        stay_qs = (
            Stay.objects.select_related('bed__room__floor__building')
            .order_by('-created_at')
        )
        qs = qs.prefetch_related(Prefetch('stays', queryset=stay_qs))
        # Prefetch bed history as well
        hist_qs = (
            TenantBedHistory.objects.select_related('bed__room__floor__building')
            .order_by('-started_on', '-created_at')
        )
        qs = qs.prefetch_related(Prefetch('bed_history', queryset=hist_qs))
        # Prefetch bookings and their invoices to avoid N+1 in TenantSerializer.get_balance
        booking_qs = (
            Booking.objects.only('id', 'tenant_id', 'status', 'start_date', 'end_date', 'booked_at')
            .order_by('-created_at')
        )
        invoice_qs = (
            Invoice.objects.only('id', 'booking_id', 'balance_due', 'status', 'cycle_month')
            .order_by('-cycle_month')
        )
        qs = qs.prefetch_related(
            Prefetch('bookings', queryset=booking_qs),
            Prefetch('bookings__invoices', queryset=invoice_qs),
        )

        params = self.request.query_params
        building = params.get('building')
        building_in = params.get('building__in')
        if building:
            qs = qs.filter(
                Q(stays__status='active', stays__bed__room__floor__building_id=building)
                | Q(building_id=building)
            )
        elif building_in:
            try:
                ids = [int(x) for x in building_in.split(',') if x.strip().isdigit()]
            except Exception:
                ids = []
            if ids:
                qs = qs.filter(
                    Q(stays__status='active', stays__bed__room__floor__building_id__in=ids)
                    | Q(building_id__in=ids)
                )

        # Enforce active buildings only: tenant must either belong to an active building
        # or have stays in an active building.
        qs = qs.filter(
            Q(building__is_active=True)
            | Q(stays__bed__room__floor__building__is_active=True)
        )

        try:
            logger.debug("TenantViewSet.get_queryset final_count=%s", qs.distinct().count())
        except Exception:
            pass
        return qs.distinct()

    def perform_create(self, serializer):
        """Enforce subscription limit for total tenants under the PG Admin owner."""
        user = getattr(self.request, 'user', None)
        owner = get_owner(user)
        # Count distinct tenants within this owner's scope (by building ownership or stays chain)
        used = (
            Tenant.objects.filter(
                Q(building__owner=owner)
                | Q(stays__bed__room__floor__building__owner=owner)
            )
            .distinct()
            .count()
        )
        ensure_limit_not_exceeded(user, 'max_tenants', used)

        # Building must be active if provided
        bld = serializer.validated_data.get('building')
        if bld and not getattr(bld, 'is_active', True):
            raise ValidationError({'building': 'Building is inactive. Activate the building to add tenants.'})

        # Enforce per-tenant media limit if media is being uploaded on create
        media_count = int(bool(serializer.validated_data.get('photo'))) + int(bool(serializer.validated_data.get('id_proof_document')))
        if media_count > 0:
            ensure_feature(user, 'tenant_media')
        per_limit = get_limit(user, 'max_tenant_media_per_tenant')
        if per_limit is not None and media_count > per_limit:
            raise ValidationError({'detail': f"Subscription limit reached for 'max_tenant_media_per_tenant' (attempted {media_count} of {per_limit})."})

        return super().perform_create(serializer)

    def perform_update(self, serializer):
        """Enforce per-tenant media limit on update when adding/replacing media."""
        user = getattr(self.request, 'user', None)
        instance: Tenant = self.get_object()
        vd = serializer.validated_data

        # Prevent assigning/moving tenant to an inactive building
        if 'building' in vd:
            bld = vd.get('building')
            if bld and not getattr(bld, 'is_active', True):
                raise ValidationError({'building': 'Building is inactive. Activate the building to assign tenants.'})

        # Only enforce when media fields are part of the update payload
        if 'photo' not in vd and 'id_proof_document' not in vd:
            return super().perform_update(serializer)

        new_photo = vd.get('photo', instance.photo)
        new_id_doc = vd.get('id_proof_document', instance.id_proof_document)
        new_count = int(bool(new_photo)) + int(bool(new_id_doc))

        # When adding new media (field was empty and now provided), enforce feature flag
        adding_photo = 'photo' in vd and bool(vd.get('photo')) and not bool(instance.photo)
        adding_id = 'id_proof_document' in vd and bool(vd.get('id_proof_document')) and not bool(instance.id_proof_document)
        if adding_photo or adding_id:
            ensure_feature(user, 'tenant_media')

        per_limit = get_limit(user, 'max_tenant_media_per_tenant')
        if per_limit is not None and new_count > per_limit:
            raise ValidationError({'detail': f"Subscription limit reached for 'max_tenant_media_per_tenant' (attempted {new_count} of {per_limit})."})

        return super().perform_update(serializer)


class EmergencyContactViewSet(viewsets.ModelViewSet):
    queryset = EmergencyContact.objects.all().order_by('tenant__id', 'name')
    serializer_class = EmergencyContactSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset().select_related('tenant', 'tenant__building')
        user = getattr(self.request, 'user', None)
        if not user or not user.is_authenticated:
            return qs.none()
        if user.is_superuser:
            return qs
        if getattr(user, 'role', None) == 'pg_admin':
            return qs.filter(Q(tenant__building__owner=user) | Q(tenant__stays__bed__room__floor__building__owner=user)).distinct()
        if getattr(user, 'role', None) == 'pg_staff' and getattr(user, 'pg_admin_id', None):
            return qs.filter(Q(tenant__building__owner_id=user.pg_admin_id) | Q(tenant__stays__bed__room__floor__building__owner_id=user.pg_admin_id)).distinct()
        return qs.none()


class StayViewSet(viewsets.ModelViewSet):
    queryset = (
        Stay.objects.select_related('tenant', 'bed', 'bed__room__floor__building')
        .all()
        .order_by('-created_at')
    )
    serializer_class = StaySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        user = getattr(self.request, 'user', None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not user.is_superuser:
            if getattr(user, 'role', None) == 'pg_admin':
                qs = qs.filter(bed__room__floor__building__owner=user)
            elif getattr(user, 'role', None) == 'pg_staff' and getattr(user, 'pg_admin_id', None):
                qs = qs.filter(bed__room__floor__building__owner_id=user.pg_admin_id)
            else:
                return qs.none()
        # Only stays in active buildings
        qs = qs.filter(bed__room__floor__building__is_active=True)
        return qs

    def perform_create(self, serializer):
        # Enforce that target building is active via selected bed
        bed = serializer.validated_data.get('bed')
        building = getattr(getattr(getattr(bed, 'room', None), 'floor', None), 'building', None) if bed else None
        if building and not getattr(building, 'is_active', True):
            raise ValidationError({'bed': 'Selected bed belongs to an inactive building. Activate the building to create stays.'})
        return super().perform_create(serializer)

    def perform_update(self, serializer):
        # Enforce active building for target bed (allow moves to active only)
        instance: Stay = self.get_object()
        bed = serializer.validated_data.get('bed', instance.bed)
        building = getattr(getattr(getattr(bed, 'room', None), 'floor', None), 'building', None) if bed else None
        if building and not getattr(building, 'is_active', True):
            raise ValidationError({'bed': 'Selected bed belongs to an inactive building. Activate the building to modify stays.'})
        return super().perform_update(serializer)


class BedHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only API for tenant bed usage history."""
    serializer_class = BedHistorySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = (
            TenantBedHistory.objects.select_related(
                'tenant', 'bed', 'bed__room__floor__building'
            )
            .all()
            .order_by('-started_on', '-created_at')
        )
        # Ownership isolation
        user = getattr(self.request, 'user', None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not user.is_superuser:
            if getattr(user, 'role', None) == 'pg_admin':
                qs = qs.filter(bed__room__floor__building__owner=user)
            elif getattr(user, 'role', None) == 'pg_staff' and getattr(user, 'pg_admin_id', None):
                qs = qs.filter(bed__room__floor__building__owner_id=user.pg_admin_id)
            else:
                return qs.none()
        # Only bed history in active buildings
        qs = qs.filter(bed__room__floor__building__is_active=True)
        params = self.request.query_params
        tenant = params.get('tenant')
        bed = params.get('bed')
        building = params.get('building')
        active = params.get('active')  # '1'/'true' to filter ended_on is null
        started_after = params.get('started_after')
        started_before = params.get('started_before')

        if tenant:
            qs = qs.filter(tenant_id=tenant)
        if bed:
            qs = qs.filter(bed_id=bed)
        if building:
            qs = qs.filter(bed__room__floor__building_id=building)
        if active and active.lower() in {"1", "true", "yes"}:
            qs = qs.filter(ended_on__isnull=True)
        if started_after:
            qs = qs.filter(started_on__gte=started_after)
        if started_before:
            qs = qs.filter(started_on__lte=started_before)
        return qs
