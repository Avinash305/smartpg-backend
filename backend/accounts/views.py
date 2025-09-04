from rest_framework import generics, permissions
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from .models import ActivityLog, log_activity, LocalizationSettings, PendingRegistration
from .serializers import (
    UserSerializer, 
    UserCreateSerializer,
    CustomTokenObtainPairSerializer,
    ActivityLogSerializer,
    LocalizationSettingsSerializer,
)
from .permissions import IsOwnerOrPGAdmin, IsSuperUser, CanAssignData
from .utils import set_user_email_otp, send_email_otp, set_email_otp_for
from datetime import timedelta
from django.contrib.auth.hashers import make_password
from subscription.utils import ensure_limit_not_exceeded, ensure_feature, compute_period_end
from subscription.models import SubscriptionPlan, Subscription

User = get_user_model()

class ActivityLogListView(generics.ListAPIView):
    serializer_class = ActivityLogSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        target_user_id = self.kwargs.get('id')
        current = self.request.user
        # Permissions: superuser can view anyone; pg_admin can view self and their staff; staff can view self
        if current.is_superuser:
            allowed = True
        elif current.role == 'pg_admin':
            allowed = str(current.id) == str(target_user_id) or User.objects.filter(id=target_user_id, pg_admin=current).exists()
        else:
            allowed = str(current.id) == str(target_user_id)
        if not allowed:
            raise PermissionDenied("Not allowed to view this user's activities")
        qs = ActivityLog.objects.filter(user_id=target_user_id).order_by('-timestamp')
        # Exclude login activities entirely
        qs = qs.exclude(action__iexact='login')
        # Optional filters for dynamic fetching
        since = self.request.query_params.get('since')
        if since:
            try:
                dt = parse_datetime(since)
                if dt is not None and timezone.is_naive(dt):
                    dt = timezone.make_aware(dt, timezone.get_current_timezone())
                if dt is not None:
                    qs = qs.filter(timestamp__gt=dt)
            except Exception:
                pass
        # Optional limit slicing (lightweight alternative to pagination)
        limit = self.request.query_params.get('limit')
        try:
            if limit is not None:
                limit_int = int(limit)
                if limit_int > 0:
                    qs = qs[:limit_int]
        except (TypeError, ValueError):
            pass
        return qs

class ActivityFeedListView(generics.ListAPIView):
    """
    Global activity feed visible to current user.
    - superuser: all users
    - pg_admin: self + their staff
    - pg_staff: self only

    Supports query params:
    - since: ISO timestamp; returns activities newer than this
    - limit: int; slice results
    - building: single building id; filters meta.building_id
    - building__in: comma-separated ids; filters meta.building_id in list
    - module: filter meta.module/type case-insensitively
    - action: filter action value case-insensitively
    """
    serializer_class = ActivityLogSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            base_qs = ActivityLog.objects.all()
        elif user.role == 'pg_admin':
            staff_ids = list(User.objects.filter(pg_admin=user).values_list('id', flat=True))
            user_ids = staff_ids + [user.id]
            base_qs = ActivityLog.objects.filter(user_id__in=user_ids)
        else:
            base_qs = ActivityLog.objects.filter(user_id=user.id)

        qs = base_qs.order_by('-timestamp')
        # Exclude login activities entirely
        qs = qs.exclude(action__iexact='login')

        # since filter
        since = self.request.query_params.get('since')
        if since:
            try:
                dt = parse_datetime(since)
                if dt is not None and timezone.is_naive(dt):
                    dt = timezone.make_aware(dt, timezone.get_current_timezone())
                if dt is not None:
                    qs = qs.filter(timestamp__gt=dt)
            except Exception:
                pass

        # building filters via JSONField meta.building_id
        building = self.request.query_params.get('building')
        building_in = self.request.query_params.get('building__in')
        if building:
            qs = qs.filter(meta__building_id=str(building))
        elif building_in:
            ids = [s for s in str(building_in).split(',') if s]
            qs = qs.filter(meta__building_id__in=ids)

        # module filter (matches meta.module or meta.type)
        module = self.request.query_params.get('module')
        if module:
            m = str(module).lower()
            qs = qs.filter(Q(meta__module__iexact=m) | Q(meta__type__iexact=m))

        # action filter
        action = self.request.query_params.get('action')
        if action:
            qs = qs.filter(action__iexact=str(action))

        # Exclude raw HTTP request logs by default (unless explicitly included). Use startswith (SQLite-safe)
        include_raw = self.request.query_params.get('include_raw')
        if not include_raw:
            qs = qs.exclude(
                Q(description__startswith='GET /') |
                Q(description__startswith='POST /') |
                Q(description__startswith='PATCH /') |
                Q(description__startswith='PUT /') |
                Q(description__startswith='DELETE /')
            )

        # limit (apply after all filtering)
        limit = self.request.query_params.get('limit')
        try:
            if limit is not None:
                limit_int = int(limit)
                if limit_int > 0:
                    qs = qs[:limit_int]
        except (TypeError, ValueError):
            pass

        return qs

class UserListView(generics.ListCreateAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrPGAdmin]
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return User.objects.all()
        elif user.role == 'pg_admin':
            # PG Admins can see themselves and their staff
            return User.objects.filter(
                Q(pk=user.pk) | 
                Q(pg_admin=user)
            )
        # Staff can see themselves and their assigned PG Admin
        return User.objects.filter(Q(pk=user.pk) | Q(pk=user.pg_admin_id))
    
    def get_serializer_class(self):
        if self.request.method == 'POST':
            return UserCreateSerializer
        return UserSerializer

    def create(self, request, *args, **kwargs):
        """
        Override create to support OTP-first flow for staff created by a PG Admin.
        - If current user is pg_admin and payload role=='pg_staff':
          Validate payload, upsert PendingRegistration with hashed password, set pg_admin,
          send OTP and return 200 with detail. Do NOT create actual User yet.
        - Otherwise fallback to default creation (e.g., superuser creating user directly).
        """
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)

        current = request.user
        role = data.get('role', 'pg_admin')
        if getattr(current, 'role', None) == 'pg_admin' and role == 'pg_staff':
            # Enforce subscription limit for number of staff under this PG Admin
            used = User.objects.filter(pg_admin=current, role='pg_staff').count()
            ensure_limit_not_exceeded(current, 'max_staff', used)

            email = data.get('email')
            if User.objects.filter(email=email).exists():
                return Response({'detail': 'An account with this email already exists.'}, status=status.HTTP_400_BAD_REQUEST)

            # Upsert PendingRegistration for staff
            pending = PendingRegistration.objects.filter(email=email).first()
            if not pending:
                pending = PendingRegistration(email=email)

            pending.full_name = data.get('full_name') or ''
            pending.phone = data.get('phone')
            pending.role = 'pg_staff'
            pending.pg_admin = current
            # Store hashed password securely
            pending.password_hash = make_password(data.get('password'))
            pending.save()

            # Generate and send OTP
            code = set_email_otp_for(pending)
            send_email_otp(pending, code)

            return Response({'detail': 'Verification code sent to staff email. Complete verification to create the staff account.'}, status=status.HTTP_200_OK)

        # Fallback: default behavior (e.g., superuser or other flows)
        return super().create(request, *args, **kwargs)

class UserDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrPGAdmin]
    lookup_field = 'id'
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return User.objects.all()
        elif user.role == 'pg_admin':
            # PG Admins can see themselves and their staff
            return User.objects.filter(
                Q(pk=user.pk) | 
                Q(pg_admin=user)
            )
        # Staff can see themselves and their assigned PG Admin
        return User.objects.filter(Q(pk=user.pk) | Q(pk=user.pg_admin_id))

    def perform_update(self, serializer):
        """
        Enforce subscription feature gating for staff profile picture uploads.
        If the incoming request attempts to set/replace 'profile_picture', require 'staff_media'.
        Deletions (clearing the field) are allowed without the feature.
        """
        request = self.request
        # Detect if a new file is being uploaded or a non-empty value is being set
        incoming_file = getattr(request, 'FILES', None).get('profile_picture') if getattr(request, 'FILES', None) else None
        incoming_val = request.data.get('profile_picture') if hasattr(request, 'data') else None
        sets_non_empty = (
            (incoming_file is not None) or (
                ('profile_picture' in request.data) and (incoming_val not in (None, '', 'null', 'None'))
            )
        )

        # Capture old file before save, so we can delete it safely after updating
        instance = serializer.instance
        old_file = getattr(instance, 'profile_picture', None)
        old_name = getattr(old_file, 'name', None)

        if sets_non_empty:
            ensure_feature(request.user, 'staff_media')

        # Perform save first
        serializer.save()

        # If a new file was uploaded OR the field was explicitly cleared, remove the old file from storage
        cleared = ('profile_picture' in request.data) and (incoming_val in (None, '', 'null', 'None'))
        replaced = incoming_file is not None
        if (cleared or replaced) and old_name:
            try:
                storage = getattr(old_file, 'storage', None)
                if storage and storage.exists(old_name):
                    storage.delete(old_name)
            except Exception:
                # Silently ignore storage deletion errors to not block profile updates
                pass

class CurrentUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    
    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        # Do not log login activities
        return response

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserCreateSerializer
    
    def get_permissions(self):
        # Only public pg_admin registrations here. Staff creation remains via protected users endpoint.
        return [permissions.AllowAny()]
    
    def create(self, request, *args, **kwargs):
        # Validate incoming fields using UserCreateSerializer without actually creating a User yet
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)

        # Enforce pg_admin role for public registration
        role = data.get('role', 'pg_admin')
        if role != 'pg_admin':
            return Response({'detail': 'Only PG Admin self-signup is allowed.'}, status=status.HTTP_400_BAD_REQUEST)

        email = data.get('email')
        if User.objects.filter(email=email).exists():
            return Response({'detail': 'An account with this email already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        # Upsert PendingRegistration for this email
        pending = PendingRegistration.objects.filter(email=email).first()
        if not pending:
            pending = PendingRegistration(email=email)

        pending.full_name = data.get('full_name') or ''
        pending.phone = data.get('phone')
        pending.role = 'pg_admin'
        # Store hashed password securely
        pending.password_hash = make_password(data.get('password'))
        pending.save()

        # Generate and send OTP
        code = set_email_otp_for(pending)
        send_email_otp(pending, code)

        return Response({'detail': 'Verification code sent to your email. Complete verification to create your account.'}, status=status.HTTP_200_OK)

class VerifyEmailOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get('email')
        code = request.data.get('code')
        if not email or not code:
            return Response({'detail': 'email and code are required'}, status=status.HTTP_400_BAD_REQUEST)

        # Prefer PendingRegistration flow
        pending = PendingRegistration.objects.filter(email=email).first()
        if pending:
            now = timezone.now()
            if not pending.email_otp or not pending.email_otp_expires_at or pending.email_otp_expires_at < now:
                return Response({'detail': 'OTP expired. Please request a new code.'}, status=status.HTTP_400_BAD_REQUEST)
            if pending.email_otp_attempts is not None and pending.email_otp_attempts >= 5:
                return Response({'detail': 'Too many attempts. Please request a new code.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            if str(code).strip() != str(pending.email_otp).strip():
                pending.email_otp_attempts = (pending.email_otp_attempts or 0) + 1
                pending.save(update_fields=['email_otp_attempts'])
                return Response({'detail': 'Invalid code'}, status=status.HTTP_400_BAD_REQUEST)

            # Success -> create actual User using hashed password
            if User.objects.filter(email=pending.email).exists():
                # In case of race/legacy
                PendingRegistration.objects.filter(pk=pending.pk).delete()
                return Response({'detail': 'Account already exists.'}, status=status.HTTP_200_OK)

            user = User(
                email=pending.email,
                full_name=pending.full_name or '',
                phone=pending.phone,
                role=pending.role or 'pg_admin',
                is_active=True,
            )
            # If staff, link to owning pg_admin
            if pending.role == 'pg_staff' and pending.pg_admin_id:
                user.pg_admin_id = pending.pg_admin_id
            # Set password hash directly
            user.password = pending.password_hash
            user.save()

            # Auto-start default free trial for new PG Admins
            try:
                if (user.role or 'pg_admin') == 'pg_admin':
                    # Only if no subscription exists yet
                    has_any_sub = Subscription.objects.filter(owner=user).exists()
                    if not has_any_sub:
                        plan = (
                            SubscriptionPlan.objects.filter(is_active=True, slug='basic').first()
                            or SubscriptionPlan.objects.filter(is_active=True).order_by('price_monthly', 'id').first()
                        )
                        if plan:
                            now = timezone.now()
                            trial_end = compute_period_end(now, '14d')
                            plan_limits = dict(plan.limits or {})
                            trial_limits = dict(plan_limits)
                            trial_limits.update({
                                'buildings': 1,
                                'max_buildings': 1,
                                'staff': 1,
                                'max_staff': 1,
                                'floors': 5,
                                'max_floors': 5,
                                'rooms': 5,
                                'max_rooms': 5,
                                'beds': 7,
                                'max_beds': 7,
                            })
                            Subscription.objects.create(
                                owner=user,
                                plan=plan,
                                status='trialing',
                                billing_interval='14d',
                                current_period_start=now,
                                current_period_end=trial_end,
                                trial_end=trial_end,
                                cancel_at_period_end=False,
                                is_current=True,
                                meta={
                                    'trial_days': 14,
                                    'features': dict(plan.features or {}),
                                    'limits': trial_limits,
                                },
                            )
            except Exception:
                # Do not block account creation if trial setup fails
                pass

            # Clean up pending
            PendingRegistration.objects.filter(pk=pending.pk).delete()
            return Response({'detail': 'Email verified successfully. Account created.'}, status=status.HTTP_200_OK)

        # Fallback: legacy flow on existing users (if any)
        user = User.objects.filter(email=email).first()
        if not user:
            return Response({'detail': 'User not found'}, status=status.HTTP_404_NOT_FOUND)
        if user.email_verified:
            return Response({'detail': 'Email already verified'}, status=status.HTTP_200_OK)
        now = timezone.now()
        if not user.email_otp or not user.email_otp_expires_at or user.email_otp_expires_at < now:
            return Response({'detail': 'OTP expired. Please request a new code.'}, status=status.HTTP_400_BAD_REQUEST)
        if user.email_otp_attempts is not None and user.email_otp_attempts >= 5:
            return Response({'detail': 'Too many attempts. Please request a new code.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if str(code).strip() != str(user.email_otp).strip():
            user.email_otp_attempts = (user.email_otp_attempts or 0) + 1
            user.save(update_fields=['email_otp_attempts'])
            return Response({'detail': 'Invalid code'}, status=status.HTTP_400_BAD_REQUEST)
        user.email_verified = True
        user.email_otp = None
        user.email_otp_expires_at = None
        user.email_otp_attempts = 0
        user.save(update_fields=['email_verified', 'email_otp', 'email_otp_expires_at', 'email_otp_attempts'])
        return Response({'detail': 'Email verified successfully'}, status=status.HTTP_200_OK)

class ResendEmailOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get('email')
        if not email:
            return Response({'detail': 'email is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Prefer pending registration first
        pending = PendingRegistration.objects.filter(email=email).first()
        if pending:
            last = pending.email_otp_last_sent_at
            if last and (timezone.now() - last) < timedelta(seconds=60):
                return Response({'detail': 'Please wait before requesting another code.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            code = set_email_otp_for(pending)
            send_email_otp(pending, code)
            return Response({'detail': 'Verification code sent'}, status=status.HTTP_200_OK)

        # Fallback to existing user (legacy)
        user = User.objects.filter(email=email).first()
        if not user:
            return Response({'detail': 'User not found'}, status=status.HTTP_404_NOT_FOUND)
        if user.email_verified:
            return Response({'detail': 'Email already verified'}, status=status.HTTP_400_BAD_REQUEST)
        last = user.email_otp_last_sent_at
        if last and (timezone.now() - last) < timedelta(seconds=60):
            return Response({'detail': 'Please wait before requesting another code.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        code = set_user_email_otp(user)
        send_email_otp(user, code)
        return Response({'detail': 'Verification code sent'}, status=status.HTTP_200_OK)

class PasswordResetRequestView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get('email')
        if not email:
            return Response({'detail': 'email is required'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email=email).first()
        # To prevent user enumeration, always return 200. Only send mail if user exists and active
        if user:
            last = user.email_otp_last_sent_at
            if last and (timezone.now() - last) < timedelta(seconds=60):
                # Still return 200 but hint cooldown
                return Response({'detail': 'If the email exists, a code was recently sent. Please wait before requesting another.'}, status=status.HTTP_200_OK)
            code = set_user_email_otp(user)
            try:
                send_email_otp(user, code)
            except Exception:
                pass
        return Response({'detail': 'If the email exists, a verification code has been sent.'}, status=status.HTTP_200_OK)

class PasswordResetVerifyOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get('email')
        code = request.data.get('code')
        new_password = request.data.get('new_password')
        if not email or not code or not new_password:
            return Response({'detail': 'email, code and new_password are required'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email=email).first()
        # Generic error to avoid enumeration timing differences
        if not user:
            return Response({'detail': 'Invalid code or expired'}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        if not user.email_otp or not user.email_otp_expires_at or user.email_otp_expires_at < now:
            return Response({'detail': 'OTP expired. Please request a new code.'}, status=status.HTTP_400_BAD_REQUEST)
        if user.email_otp_attempts is not None and user.email_otp_attempts >= 5:
            return Response({'detail': 'Too many attempts. Please request a new code.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if str(code).strip() != str(user.email_otp).strip():
            user.email_otp_attempts = (user.email_otp_attempts or 0) + 1
            user.save(update_fields=['email_otp_attempts'])
            return Response({'detail': 'Invalid code'}, status=status.HTTP_400_BAD_REQUEST)

        # Set the new password and clear OTP fields
        user.set_password(new_password)
        user.email_otp = None
        user.email_otp_expires_at = None
        user.email_otp_last_sent_at = None
        user.email_otp_attempts = 0
        user.save(update_fields=['password', 'email_otp', 'email_otp_expires_at', 'email_otp_last_sent_at', 'email_otp_attempts'])

        return Response({'detail': 'Password has been reset successfully.'}, status=status.HTTP_200_OK)

class LocalizationSettingsViewSet(viewsets.ModelViewSet):
    serializer_class = LocalizationSettingsSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return LocalizationSettings.objects.all()
        # For pg_admin: their own record; for staff: their admin's record
        owner_id = user.id if user.role == 'pg_admin' else getattr(user, 'pg_admin_id', None)
        return LocalizationSettings.objects.filter(owner_id=owner_id)

    def perform_create(self, serializer):
        user = self.request.user
        if not (user.is_superuser or user.role == 'pg_admin'):
            raise PermissionDenied("Only PG Admins can create localization settings.")
        serializer.save(owner=user)

    def perform_update(self, serializer):
        user = self.request.user
        instance: LocalizationSettings = self.get_object()
        if not (user.is_superuser or (user.role == 'pg_admin' and instance.owner_id == user.id)):
            raise PermissionDenied("You cannot modify these localization settings.")
        serializer.save()

    @action(detail=False, methods=['get'], url_path='current')
    def current(self, request):
        qs = self.get_queryset()
        obj = qs.first()
        if not obj:
            return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)
        ser = self.get_serializer(obj)
        return Response(ser.data)

class ActivityLogDismissView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, id: int, activity_id: int):
        """
        Dismiss (delete) a specific activity for the given user.
        Permissions mirror ActivityLogListView: superuser can act on anyone; pg_admin on self and staff; staff on self.
        """
        target_user_id = id
        current = request.user
        # Permission check
        if current.is_superuser:
            allowed = True
        elif current.role == 'pg_admin':
            allowed = str(current.id) == str(target_user_id) or User.objects.filter(id=target_user_id, pg_admin=current).exists()
        else:
            allowed = str(current.id) == str(target_user_id)
        if not allowed:
            raise PermissionDenied("Not allowed to modify this user's activities")

        obj = ActivityLog.objects.filter(id=activity_id, user_id=target_user_id).first()
        if not obj:
            return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)

        # Do not dismiss unpaid invoice dues
        try:
            meta = obj.meta or {}
            mtype = str(meta.get('type') or meta.get('category') or '').lower()
            module = str(meta.get('module') or meta.get('model') or '').lower()
            has_invoice_obj = any(k in meta for k in ('invoice', 'invoice_id', 'invoiceId'))
            looks_invoice = (
                mtype == 'invoice' or 'invoice' in (obj.action or '').lower() or module in ('invoice', 'invoices', 'billing') or has_invoice_obj
            )
            # unpaid?
            inv = meta.get('invoice') or {}
            paid_flags = [meta.get('paid'), inv.get('paid'), meta.get('is_paid'), inv.get('is_paid')]
            paid_at = meta.get('paid_at') or inv.get('paid_at')
            status_val = str(meta.get('status') or meta.get('payment_status') or inv.get('status') or inv.get('payment_status') or '').lower()
            numeric_due = meta.get('balance_due') or meta.get('amount_due') or meta.get('remaining') or inv.get('balance_due') or inv.get('amount_due') or inv.get('remaining')
            unpaid_by_flag = any(p is False for p in paid_flags)
            unpaid_by_amount = False
            try:
                unpaid_by_amount = float(numeric_due) > 0 if numeric_due is not None else False
            except Exception:
                unpaid_by_amount = False
            unpaid_by_status = status_val in ('unpaid', 'due', 'overdue', 'partial', 'pending')
            is_unpaid = (unpaid_by_flag or unpaid_by_amount or unpaid_by_status) and not paid_at
            if looks_invoice and is_unpaid:
                return Response({"detail": "Cannot dismiss unpaid invoice due"}, status=status.HTTP_409_CONFLICT)
        except Exception:
            pass

        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)