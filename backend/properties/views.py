from django.shortcuts import render
from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework import permissions
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Building, Floor, Room, Bed
from .serializers import BuildingSerializer, FloorSerializer, RoomSerializer, BedSerializer
from tenants.serializers import BedHistorySerializer
from accounts.permissions import ensure_staff_module_permission
from subscription.utils import ensure_limit_not_exceeded

# Create your views here.

class BuildingViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = BuildingSerializer
    queryset = Building.objects.all().select_related('owner', 'manager').order_by('name')

    def get_queryset(self):
        qs = super().get_queryset()
        user = getattr(self.request, 'user', None)

        # Optional is_active filter from query params
        active_param = self.request.query_params.get('is_active')
        def apply_active(qs_in):
            if active_param is None:
                return qs_in
            val = str(active_param).lower() in ('1', 'true', 'yes', 'y', 't')
            return qs_in.filter(is_active=val)

        if not user or not user.is_authenticated:
            return qs.none()
        if user.is_superuser:
            owner = self.request.query_params.get('owner')
            if owner:
                return apply_active(qs.filter(owner_id=owner))
            return apply_active(qs)
        if getattr(user, 'role', None) == 'pg_admin':
            qs = qs.filter(owner=user)
            # Optional defensive filter: if 'owner' is provided and does not match current admin, return none
            owner = self.request.query_params.get('owner')
            if owner and str(owner) != str(user.id):
                return qs.none()
            return apply_active(qs)
        # pg_staff: restrict to their admin's data
        if getattr(user, 'role', None) == 'pg_staff' and getattr(user, 'pg_admin_id', None):
            # Ignore any provided owner filter for staff
            qs = qs.filter(owner_id=user.pg_admin_id)
            # Enforce staff 'view' permission (global fallback across any building)
            if not ensure_staff_module_permission(user, 'buildings', 'view'):
                return qs.none()
            return apply_active(qs)
        return qs.none()

    def perform_create(self, serializer):
        user = self.request.user
        # Superuser: allow specifying owner; default to current user if not provided
        if user.is_superuser:
            owner = serializer.validated_data.get('owner') or user
            used = Building.objects.filter(owner=owner, is_active=True).count()
            ensure_limit_not_exceeded(owner, 'max_buildings', used)
            serializer.save(owner=owner)
            return
        role = getattr(user, 'role', None)
        # pg_admin: force owner to current admin regardless of payload
        if role == 'pg_admin':
            used = Building.objects.filter(owner=user, is_active=True).count()
            ensure_limit_not_exceeded(user, 'max_buildings', used)
            serializer.save(owner=user)
            return
        # pg_staff: allow only within their pg_admin scope and if they have 'add' permission
        if role == 'pg_staff' and getattr(user, 'pg_admin_id', None):
            # Staff cannot choose arbitrary owner; force to their admin
            if not ensure_staff_module_permission(user, 'buildings', 'add'):
                raise PermissionDenied('You do not have permission to add buildings.')
            target_owner = user.pg_admin
            used = Building.objects.filter(owner=target_owner, is_active=True).count()
            ensure_limit_not_exceeded(target_owner, 'max_buildings', used)
            serializer.save(owner_id=user.pg_admin_id)
            return
        raise PermissionDenied('Only authorized users can create buildings.')

    def perform_update(self, serializer):
        user = self.request.user
        instance = self.get_object()
        if user.is_superuser:
            serializer.save()
            return
        role = getattr(user, 'role', None)
        # pg_admin: can edit their own buildings; prevent owner reassignment
        if role == 'pg_admin' and instance.owner_id == user.id:
            # If toggling inactive -> active, enforce active building limit
            will_be_active = serializer.validated_data.get('is_active', instance.is_active)
            if not instance.is_active and will_be_active:
                used = Building.objects.filter(owner=user, is_active=True).count()
                ensure_limit_not_exceeded(user, 'max_buildings', used)
            serializer.save(owner=instance.owner)
            return
        # pg_staff: can edit buildings of their pg_admin with explicit 'edit' permission
        if role == 'pg_staff' and instance.owner_id == getattr(user, 'pg_admin_id', None):
            if not ensure_staff_module_permission(user, 'buildings', 'edit', building_id=instance.id):
                # building_id here refers to Building.pk; permission checker normalizes key
                raise PermissionDenied('You do not have permission to edit buildings.')
            # If toggling inactive -> active, enforce active building limit for their admin
            will_be_active = serializer.validated_data.get('is_active', instance.is_active)
            if not instance.is_active and will_be_active:
                target_owner = instance.owner
                used = Building.objects.filter(owner=target_owner, is_active=True).count()
                ensure_limit_not_exceeded(target_owner, 'max_buildings', used)
            # Prevent owner reassignment
            serializer.save(owner=instance.owner)
            return
        raise PermissionDenied('You cannot modify this building.')

    def perform_destroy(self, instance):
        user = self.request.user
        if user.is_superuser:
            instance.delete()
            return
        role = getattr(user, 'role', None)
        # pg_admin can delete their own buildings
        if role == 'pg_admin' and instance.owner_id == user.id:
            instance.delete()
            return
        # pg_staff: can delete within their pg_admin scope if permitted
        if role == 'pg_staff' and instance.owner_id == getattr(user, 'pg_admin_id', None):
            if ensure_staff_module_permission(user, 'buildings', 'delete', building_id=instance.id):
                instance.delete()
                return
            raise PermissionDenied('You do not have permission to delete buildings.')
        raise PermissionDenied('You cannot delete this building.')


class FloorViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = FloorSerializer
    queryset = Floor.objects.all().select_related('building').order_by('building__name', 'number')

    def get_queryset(self):
        qs = super().get_queryset()
        # Ownership isolation
        user = getattr(self.request, 'user', None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not user.is_superuser:
            if getattr(user, 'role', None) == 'pg_admin':
                qs = qs.filter(building__owner=user)
            elif getattr(user, 'role', None) == 'pg_staff' and getattr(user, 'pg_admin_id', None):
                qs = qs.filter(building__owner_id=user.pg_admin_id)
                # Enforce staff 'view' permission (global fallback)
                if not ensure_staff_module_permission(user, 'floors', 'view'):
                    return qs.none()
            else:
                return qs.none()
        building = self.request.query_params.get('building')
        if building:
            qs = qs.filter(building_id=building)
        active_param = self.request.query_params.get('is_active')
        if active_param is not None:
            val = str(active_param).lower() in ('1', 'true', 'yes', 'y', 't')
            qs = qs.filter(is_active=val)
        return qs

    def _ensure_admin_owns_building(self, building):
        user = self.request.user
        if user.is_superuser:
            return
        role = getattr(user, 'role', None)
        # Allow pg_admin on their own buildings
        if role == 'pg_admin' and building.owner_id == user.id:
            return
        # Allow pg_staff when the building belongs to their pg_admin
        if role == 'pg_staff' and building.owner_id == getattr(user, 'pg_admin_id', None):
            return
        raise PermissionDenied('You can only create/update floors within your PG Admin\'s buildings.')

    def perform_create(self, serializer):
        user = self.request.user
        # Allow superuser/pg_admin. For pg_staff, require JSON permission.
        if not (user.is_superuser or getattr(user, 'role', None) in ('pg_admin', 'pg_staff')):
            raise PermissionDenied('You are not allowed to create floors.')
        building = serializer.validated_data.get('building')
        if not building:
            raise ValidationError({'building': 'This field is required.'})
        # Ownership check for admin and staff
        self._ensure_admin_owns_building(building)
        if getattr(user, 'role', None) == 'pg_staff':
            # Staff can only act within their admin's buildings and with permission
            if building.owner_id != getattr(user, 'pg_admin_id', None):
                raise PermissionDenied('You can only create floors within your PG Admin\'s buildings.')
            if not ensure_staff_module_permission(user, 'floors', 'add', building_id=building.id):
                raise PermissionDenied('You do not have permission to add floors.')
        # Enforce subscription limit for number of floors within a building
        used = Floor.objects.filter(building=building).count()
        ensure_limit_not_exceeded(building.owner, 'max_floors_per_building', used)
        serializer.save()

    def perform_update(self, serializer):
        user = self.request.user
        instance = self.get_object()
        if user.is_superuser:
            serializer.save()
            return
        role = getattr(user, 'role', None)
        if role == 'pg_admin' and instance.building.owner_id == user.id:
            pass
        elif role == 'pg_staff' and instance.building.owner_id == getattr(user, 'pg_admin_id', None):
            # Check JSON edit permission (use target building after potential change)
            building = serializer.validated_data.get('building', instance.building)
            if not ensure_staff_module_permission(user, 'floors', 'edit', building_id=building.id):
                raise PermissionDenied('You do not have permission to edit floors.')
        else:
            raise PermissionDenied('You cannot modify this floor.')
        building = serializer.validated_data.get('building', instance.building)
        self._ensure_admin_owns_building(building)
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if user.is_superuser:
            instance.delete()
            return
        role = getattr(user, 'role', None)
        if role == 'pg_admin' and instance.building.owner_id == user.id:
            instance.delete()
            return
        if role == 'pg_staff' and instance.building.owner_id == getattr(user, 'pg_admin_id', None):
            if ensure_staff_module_permission(user, 'floors', 'delete', building_id=instance.building.id):
                instance.delete()
                return
            raise PermissionDenied('You do not have permission to delete floors.')
        raise PermissionDenied('You cannot delete this floor.')


class RoomViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = RoomSerializer
    queryset = (
        Room.objects.all()
        .select_related('floor', 'floor__building')
        .order_by('floor__building__name', 'floor__number', 'number')
    )

    def get_queryset(self):
        qs = super().get_queryset()
        # Ownership isolation
        user = getattr(self.request, 'user', None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not user.is_superuser:
            if getattr(user, 'role', None) == 'pg_admin':
                qs = qs.filter(floor__building__owner=user)
            elif getattr(user, 'role', None) == 'pg_staff' and getattr(user, 'pg_admin_id', None):
                qs = qs.filter(floor__building__owner_id=user.pg_admin_id)
                if not ensure_staff_module_permission(user, 'rooms', 'view'):
                    return qs.none()
            else:
                return qs.none()
        floor = self.request.query_params.get('floor')
        if floor:
            qs = qs.filter(floor_id=floor)
        building = self.request.query_params.get('building')
        if building:
            qs = qs.filter(floor__building_id=building)
        return qs

    def _ensure_admin_owns_floor(self, floor):
        user = self.request.user
        if user.is_superuser:
            return
        role = getattr(user, 'role', None)
        # Allow pg_admin on their own buildings
        if role == 'pg_admin' and floor.building.owner_id == user.id:
            return
        # Allow pg_staff when the floor's building belongs to their pg_admin
        if role == 'pg_staff' and floor.building.owner_id == getattr(user, 'pg_admin_id', None):
            return
        raise PermissionDenied('You can only create/update rooms within your PG Admin\'s buildings.')

    def perform_create(self, serializer):
        user = self.request.user
        if not (user.is_superuser or getattr(user, 'role', None) in ('pg_admin', 'pg_staff')):
            raise PermissionDenied('You are not allowed to create rooms.')
        floor = serializer.validated_data.get('floor')
        if not floor:
            raise ValidationError({'floor': 'This field is required.'})
        self._ensure_admin_owns_floor(floor)
        if getattr(user, 'role', None) == 'pg_staff':
            if floor.building.owner_id != getattr(user, 'pg_admin_id', None):
                raise PermissionDenied('You can only create rooms within your PG Admin\'s buildings.')
            if not ensure_staff_module_permission(user, 'rooms', 'add', building_id=floor.building.id):
                raise PermissionDenied('You do not have permission to add rooms.')
        # Enforce subscription limit for number of rooms within a floor
        used = Room.objects.filter(floor=floor).count()
        ensure_limit_not_exceeded(floor.building.owner, 'max_rooms_per_floor', used)
        serializer.save()

    def perform_update(self, serializer):
        user = self.request.user
        instance = self.get_object()
        if user.is_superuser:
            serializer.save()
            return
        role = getattr(user, 'role', None)
        if role == 'pg_admin' and instance.floor.building.owner_id == user.id:
            pass
        elif role == 'pg_staff' and instance.floor.building.owner_id == getattr(user, 'pg_admin_id', None):
            target_floor = serializer.validated_data.get('floor', instance.floor)
            if not ensure_staff_module_permission(user, 'rooms', 'edit', building_id=target_floor.building.id):
                raise PermissionDenied('You do not have permission to edit rooms.')
        else:
            raise PermissionDenied('You cannot modify this room.')
        floor = serializer.validated_data.get('floor', instance.floor)
        self._ensure_admin_owns_floor(floor)
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if user.is_superuser:
            instance.delete()
            return
        role = getattr(user, 'role', None)
        if role == 'pg_admin' and instance.floor.building.owner_id == user.id:
            instance.delete()
            return
        if role == 'pg_staff' and instance.floor.building.owner_id == getattr(user, 'pg_admin_id', None):
            if ensure_staff_module_permission(user, 'rooms', 'delete', building_id=instance.floor.building.id):
                instance.delete()
                return
            raise PermissionDenied('You do not have permission to delete rooms.')
        raise PermissionDenied('You cannot delete this room.')


class BedViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = BedSerializer
    queryset = (
        Bed.objects.all()
        .select_related('room', 'room__floor', 'room__floor__building')
        .order_by('room__floor__building__name', 'room__floor__number', 'room__number', 'number')
    )

    def get_queryset(self):
        qs = super().get_queryset()
        # Ownership isolation
        user = getattr(self.request, 'user', None)
        if not user or not user.is_authenticated:
            return qs.none()
        if not user.is_superuser:
            if getattr(user, 'role', None) == 'pg_admin':
                qs = qs.filter(room__floor__building__owner=user)
            elif getattr(user, 'role', None) == 'pg_staff' and getattr(user, 'pg_admin_id', None):
                qs = qs.filter(room__floor__building__owner_id=user.pg_admin_id)
                if not ensure_staff_module_permission(user, 'beds', 'view'):
                    return qs.none()
            else:
                return qs.none()
        room = self.request.query_params.get('room')
        if room:
            qs = qs.filter(room_id=room)
        floor = self.request.query_params.get('floor')
        if floor:
            qs = qs.filter(room__floor_id=floor)
        building = self.request.query_params.get('building')
        if building:
            qs = qs.filter(room__floor__building_id=building)
        return qs

    def _ensure_admin_owns_room(self, room):
        user = self.request.user
        if user.is_superuser:
            return
        role = getattr(user, 'role', None)
        # Allow pg_admin on their own buildings
        if role == 'pg_admin' and room.floor.building.owner_id == user.id:
            return
        # Allow pg_staff when the room's building belongs to their pg_admin
        if role == 'pg_staff' and room.floor.building.owner_id == getattr(user, 'pg_admin_id', None):
            return
        raise PermissionDenied('You can only create/update beds within your PG Admin\'s buildings.')

    def perform_create(self, serializer):
        user = self.request.user
        if not (user.is_superuser or getattr(user, 'role', None) in ('pg_admin', 'pg_staff')):
            raise PermissionDenied('You are not allowed to create beds.')
        room = serializer.validated_data.get('room')
        if not room:
            raise ValidationError({'room': 'This field is required.'})
        self._ensure_admin_owns_room(room)
        if getattr(user, 'role', None) == 'pg_staff':
            if room.floor.building.owner_id != getattr(user, 'pg_admin_id', None):
                raise PermissionDenied('You can only create beds within your PG Admin\'s buildings.')
            if not ensure_staff_module_permission(user, 'beds', 'add', building_id=room.floor.building.id):
                raise PermissionDenied('You do not have permission to add beds.')
        # Enforce subscription limit for number of beds within a room
        used = Bed.objects.filter(room=room).count()
        ensure_limit_not_exceeded(room.floor.building.owner, 'max_beds_per_room', used)
        serializer.save()

    def perform_update(self, serializer):
        user = self.request.user
        instance = self.get_object()
        if user.is_superuser:
            serializer.save()
            return
        role = getattr(user, 'role', None)
        if role == 'pg_admin' and instance.room.floor.building.owner_id == user.id:
            pass
        elif role == 'pg_staff' and instance.room.floor.building.owner_id == getattr(user, 'pg_admin_id', None):
            target_room = serializer.validated_data.get('room', instance.room)
            if not ensure_staff_module_permission(user, 'beds', 'edit', building_id=target_room.floor.building.id):
                raise PermissionDenied('You do not have permission to edit beds.')
        else:
            raise PermissionDenied('You cannot modify this bed.')
        room = serializer.validated_data.get('room', instance.room)
        self._ensure_admin_owns_room(room)
        serializer.save()

    @action(detail=True, methods=['get'], url_path='history')
    def history(self, request, pk=None):
        """Return tenants who previously/currently stayed in this bed (history rows, newest first)."""
        bed = self.get_object()
        # Preload all relations used by the serializer to avoid N+1 queries
        qs = (
            bed.usage_history
            .select_related('tenant', 'bed', 'bed__room', 'bed__room__floor', 'bed__room__floor__building')
            .order_by('-started_on', '-created_at')
        )
        serializer = BedHistorySerializer(qs, many=True)
        return Response(serializer.data)

    def perform_destroy(self, instance):
        user = self.request.user
        if user.is_superuser:
            instance.delete()
            return
        role = getattr(user, 'role', None)
        if role == 'pg_admin' and instance.room.floor.building.owner_id == user.id:
            instance.delete()
            return
        if role == 'pg_staff' and instance.room.floor.building.owner_id == getattr(user, 'pg_admin_id', None):
            if ensure_staff_module_permission(user, 'beds', 'delete', building_id=instance.room.floor.building.id):
                instance.delete()
                return
            raise PermissionDenied('You do not have permission to delete beds.')
        raise PermissionDenied('You cannot delete this bed.')
