from django.utils import timezone
from rest_framework import serializers, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Q
from accounts.permissions import ensure_staff_module_permission, MODULES
from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse

from .models import Notification


# ----- Serializers (kept local to avoid creating new files) -----
class NotificationSerializer(serializers.ModelSerializer):
    subject_type = serializers.SerializerMethodField()
    created_at_display = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "event",
            "title",
            "message",
            "level",
            "unread",
            "read_at",
            "created_at",
            "created_at_display",
            "subject_type",
            "subject_object_id",
            "payload",
            "channels",
        ]
        read_only_fields = fields

    def get_subject_type(self, obj):
        return obj.subject_content_type.model if obj.subject_content_type else None

    def get_created_at_display(self, obj):
        # Asia/Kolkata and dd:mm:yyyy hh:mm:ss am/pm
        dt = timezone.localtime(obj.created_at)
        return dt.strftime("%d:%m:%Y %I:%M:%S %p")


class MarkReadSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False
    )


# ----- Permissions -----
class IsAuthenticatedRecipient(permissions.BasePermission):
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated


class IsPGAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and (request.user.is_superuser or getattr(request.user, "role", None) == "pg_admin"))


class IsPGStaff(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and getattr(request.user, "role", None) == "pg_staff")


# ----- Views -----
@extend_schema(
    parameters=[
        OpenApiParameter(name="unread", type=str, description="Filter unread only (true/1)."),
        OpenApiParameter(name="page", type=int, description="Page number (default 1)."),
        OpenApiParameter(name="page_size", type=int, description="Items per page (max 100, default 20)."),
    ],
    responses={200: OpenApiResponse(response=NotificationSerializer, description="Paginated notifications for the current user")},
)
class NotificationListView(APIView):
    permission_classes = [IsAuthenticatedRecipient]

    def get(self, request):
        unread = request.query_params.get("unread")
        qs = Notification.objects.filter(recipient=request.user)
        if unread in {"1", "true", "True"}:
            qs = qs.filter(unread=True)
        qs = qs.order_by("-created_at")
        page = int(request.query_params.get("page", 1))
        page_size = min(int(request.query_params.get("page_size", 20)), 100)
        start = (page - 1) * page_size
        end = start + page_size
        serializer = NotificationSerializer(qs[start:end], many=True)
        return Response({
            "count": qs.count(),
            "results": serializer.data,
        })


@extend_schema(responses={200: OpenApiResponse(description="Unread count for the current user", response=dict)})
class NotificationUnreadCountView(APIView):
    permission_classes = [IsAuthenticatedRecipient]

    def get(self, request):
        count = Notification.objects.filter(recipient=request.user, unread=True).count()
        return Response({"unread": count})


@extend_schema(request=MarkReadSerializer, responses={200: OpenApiResponse(description="Number of notifications marked as read", response=dict)})
class NotificationMarkReadView(APIView):
    permission_classes = [IsAuthenticatedRecipient]

    def post(self, request):
        serializer = MarkReadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["ids"]
        qs = Notification.objects.filter(recipient=request.user, id__in=ids, unread=True)
        now = timezone.now()
        updated = qs.update(unread=False, read_at=now)
        return Response({"updated": updated})


@extend_schema(responses={200: OpenApiResponse(description="Number of notifications marked as read", response=dict)})
class NotificationMarkAllReadView(APIView):
    permission_classes = [IsAuthenticatedRecipient]

    def post(self, request):
        qs = Notification.objects.filter(recipient=request.user, unread=True)
        now = timezone.now()
        updated = qs.update(unread=False, read_at=now)
        return Response({"updated": updated})


@extend_schema(
    parameters=[
        OpenApiParameter(name="unread", type=str, description="Filter unread only (true/1)."),
        OpenApiParameter(name="building", type=int, description="Building id to filter within the org."),
        OpenApiParameter(name="page", type=int),
        OpenApiParameter(name="page_size", type=int),
    ],
    responses={200: OpenApiResponse(response=NotificationSerializer)},
)
class OrgNotificationListView(APIView):
    """List notifications for a PG Admin's organization, scoped by pg_admin and optional building filter."""
    permission_classes = [IsPGAdmin]

    def get(self, request):
        unread = request.query_params.get("unread")
        building_id = request.query_params.get("building")
        qs = Notification.objects.filter(pg_admin=request.user)
        if building_id:
            qs = qs.filter(building_id=building_id)
        if unread in {"1", "true", "True"}:
            qs = qs.filter(unread=True)
        qs = qs.order_by("-created_at")
        page = int(request.query_params.get("page", 1))
        page_size = min(int(request.query_params.get("page_size", 20)), 100)
        start = (page - 1) * page_size
        end = start + page_size
        serializer = NotificationSerializer(qs[start:end], many=True)
        return Response({
            "count": qs.count(),
            "results": serializer.data,
        })


@extend_schema(
    parameters=[
        OpenApiParameter(name="unread", type=str, description="Filter unread only (true/1)."),
        OpenApiParameter(name="building", type=int, description="Restrict to a permitted building id."),
        OpenApiParameter(name="page", type=int),
        OpenApiParameter(name="page_size", type=int),
    ],
    responses={200: OpenApiResponse(response=NotificationSerializer)},
)
class StaffNotificationListView(APIView):
    """List notifications for PG Staff limited to buildings they have view permission for.

    A staff user can see notifications where Notification.building is among buildings
    they have 'view' permission for at least one module (e.g., bookings, payments, tenants, etc.).
    Optional query param `building` further narrows to a single building if allowed.
    """
    permission_classes = [IsPGStaff]

    def _allowed_building_ids(self, user) -> set[str]:
        perms = getattr(user, "permissions", {}) or {}
        allowed: set[str] = set()
        for bkey, modmap in perms.items():
            if bkey == "global":
                continue
            try:
                int(bkey)
            except Exception:
                # skip non-numeric keys for building scope
                continue
            # if any module grants view=True, consider the building visible for notifications
            for mod in MODULES:
                try:
                    m = modmap.get(mod, {})
                    if isinstance(m.get("view"), bool) and m.get("view"):
                        allowed.add(bkey)
                        break
                except AttributeError:
                    continue
        return allowed

    def get(self, request):
        user = request.user
        building_param = request.query_params.get("building")
        allowed = self._allowed_building_ids(user)
        if building_param:
            # restrict to this building only if allowed
            if building_param not in allowed:
                return Response({"detail": "Not permitted for this building."}, status=status.HTTP_403_FORBIDDEN)
            building_ids = [building_param]
        else:
            building_ids = list(allowed)

        qs = Notification.objects.filter(building_id__in=building_ids)
        unread = request.query_params.get("unread")
        if unread in {"1", "true", "True"}:
            qs = qs.filter(unread=True)
        qs = qs.order_by("-created_at")
        page = int(request.query_params.get("page", 1))
        page_size = min(int(request.query_params.get("page_size", 20)), 100)
        start = (page - 1) * page_size
        end = start + page_size
        serializer = NotificationSerializer(qs[start:end], many=True)
        return Response({
            "count": qs.count(),
            "results": serializer.data,
        })
