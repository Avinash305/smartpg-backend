from rest_framework import serializers
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from .models import ActivityLog, LocalizationSettings

User = get_user_model()

class ActivityLogSerializer(serializers.ModelSerializer):
    # Derived top-level fields for frontend convenience
    module = serializers.SerializerMethodField()
    actor_name = serializers.SerializerMethodField()
    target_name = serializers.SerializerMethodField()
    building_name = serializers.SerializerMethodField()
    # Common payment chips
    amount = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    method = serializers.SerializerMethodField()
    reference = serializers.SerializerMethodField()
    period_label = serializers.SerializerMethodField()

    class Meta:
        model = ActivityLog
        fields = (
            "id",
            "action",
            "description",
            "timestamp",
            "meta",
            # Flattened helpers
            "module",
            "actor_name",
            "target_name",
            "building_name",
            "amount",
            "currency",
            "method",
            "reference",
            "period_label",
        )

    def _m(self, obj):
        return getattr(obj, "meta", {}) or {}

    def get_module(self, obj):
        m = self._m(obj)
        # common fallbacks across modules
        return (
            (m.get("module") or m.get("type") or m.get("model") or m.get("category"))
        )

    def get_actor_name(self, obj):
        # Prefer explicit meta override, else user full_name/email fallback
        m = self._m(obj)
        if m.get("actor_name"):
            return m.get("actor_name")
        u = getattr(obj, "user", None)
        if u:
            return getattr(u, "full_name", None) or getattr(u, "email", None)
        return None

    def _first(self, dct, keys):
        for k in keys:
            val = dct.get(k)
            if val not in (None, ""):
                return val
        return None

    def get_target_name(self, obj):
        m = self._m(obj)
        # Try standard keys
        val = self._first(m, [
            "target_name", "tenant_name", "invoice_label", "room_name", "bed_name", "name", "title",
        ])
        if val:
            return val
        # Try nested meta objects
        for n in ("payment", "invoice", "booking", "tenant", "room", "bed", "building"):
            sub = m.get(n) or {}
            val = self._first(sub, [
                "target_name", "tenant_name", "invoice_label", "room_name", "bed_name", "name", "title",
            ])
            if val:
                return val
        return None

    def get_building_name(self, obj):
        m = self._m(obj)
        # direct
        val = self._first(m, ["building_name", "pg_name", "property_name"])  # common aliases
        if val:
            return val
        # nested building object
        b = m.get("building") or {}
        val = self._first(b, ["name", "building_name", "title"]) or None
        return val

    def get_amount(self, obj):
        m = self._m(obj)
        p = m.get("payment") or {}
        # Return as string to avoid float issues
        return p.get("amount") or m.get("amount")

    def get_currency(self, obj):
        m = self._m(obj)
        return m.get("currency") or m.get("ccy") or "INR"

    def get_method(self, obj):
        m = self._m(obj)
        p = m.get("payment") or {}
        return p.get("method") or m.get("method") or m.get("payment_method") or m.get("mode")

    def get_reference(self, obj):
        m = self._m(obj)
        p = m.get("payment") or {}
        return (
            p.get("reference")
            or m.get("reference")
            or m.get("utr")
            or m.get("txn_id")
            or m.get("transaction_id")
            or m.get("ref_no")
            or m.get("receipt_no")
        )

    def get_period_label(self, obj):
        m = self._m(obj)
        return (
            m.get("period_label")
            or (m.get("invoice") or {}).get("period_label")
            or None
        )

class UserSerializer(serializers.ModelSerializer):
    pg_admin_info = serializers.SerializerMethodField()
    # Allow toggling active status via PATCH
    is_active = serializers.BooleanField(required=False)
    # Expose profile picture for upload/update
    profile_picture = serializers.ImageField(required=False, allow_null=True)
    
    class Meta:
        model = User
        fields = (
            'id', 'email', 'full_name', 'phone', 'role',
            'is_active', 'is_staff', 'date_joined', 'hierarchical_id',
            'pg_admin', 'pg_admin_info',
            'permissions',
            'language',
            'email_verified',
            'profile_picture',
        )
        read_only_fields = (
            'id', 'is_staff', 'date_joined', 
            'hierarchical_id', 'pg_admin_info'
        )
        extra_kwargs = {
            'pg_admin': {'required': False},
            'profile_picture': {'required': False, 'allow_null': True},
        }
    
    def get_pg_admin_info(self, obj):
        if obj.pg_admin:
            return {
                'id': obj.pg_admin.id,
                'email': obj.pg_admin.email,
                'name': obj.pg_admin.full_name or ''
            }
        return None
    
    def validate(self, attrs):
        request = self.context.get('request')
        if request and request.method in ['PUT', 'PATCH']:
            # Prevent users from changing their role or pg_admin unless they're superusers
            if 'role' in attrs and not request.user.is_superuser:
                attrs.pop('role')
            if 'pg_admin' in attrs and not request.user.is_superuser:
                attrs.pop('pg_admin')
        return attrs

class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    password2 = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    phone = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    pg_admin = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(role='pg_admin'),
        required=False,
        allow_null=True
    )

    class Meta:
        model = User
        fields = ('email', 'password', 'password2', 'full_name', 'phone', 'role', 'pg_admin')
        extra_kwargs = {
            'password': {'write_only': True},
            'password2': {'write_only': True},
            'phone': {'required': False},
            'role': {'default': 'pg_admin'}
        }

    def validate(self, attrs):
        # Check if passwords match
        if attrs['password'] != attrs.pop('password2'):
            raise serializers.ValidationError({"password": "Password fields didn't match."})
            
        # Check role-based validation
        request = self.context.get('request')
        role = attrs.get('role', 'pg_admin')
        
        # If creating a staff account, pg_admin is required
        if role == 'pg_staff' and not attrs.get('pg_admin') and request and request.user.is_authenticated:
            # If pg_admin not provided, use the current user if they're a pg_admin
            if request.user.role == 'pg_admin':
                attrs['pg_admin'] = request.user
            else:
                raise serializers.ValidationError({"pg_admin": "This field is required for staff accounts."})
        
        # If creating a pg_admin account, ensure pg_admin is not set
        if role == 'pg_admin':
            attrs.pop('pg_admin', None)
            
        return attrs

    def create(self, validated_data):
        # Remove password2 before creating user
        validated_data.pop('password2', None)
        
        # Set is_active to True by default
        validated_data['is_active'] = True
        
        # Create the user
        user = User.objects.create_user(**validated_data)
        return user

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = 'email'  # Use email as the username field
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Remove the username field and add email field
        self.fields.pop('username', None)
        self.fields['email'] = serializers.EmailField(required=True)
        self.fields['password'] = serializers.CharField(required=True, write_only=True)

    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')
        
        if not email or not password:
            raise serializers.ValidationError({
                'detail': 'Both email and password are required.'
            })
            
        try:
            # Try to get the user by email
            user = User.objects.get(email=email)
            
            # Verify the password
            if not user.check_password(password):
                raise serializers.ValidationError({
                    'detail': 'Invalid credentials.'
                })
            
            # Block inactive users from obtaining tokens
            if not user.is_active:
                raise serializers.ValidationError({
                    'detail': 'Your account is inactive. Please contact the administrator.'
                })
            
            # Note: Previously, unverified emails were blocked from login. Requirement changed to allow login without email verification.
             
            # Generate the token
            refresh = self.get_token(user)
            
            data = {}
            data['refresh'] = str(refresh)
            data['access'] = str(refresh.access_token)
            data['user'] = {
                'id': user.id,
                'email': user.email,
                'full_name': user.full_name,
                'role': user.role,
                'is_active': user.is_active,
                'language': getattr(user, 'language', 'en'),
                'email_verified': getattr(user, 'email_verified', False),
            }
            
            return data
            
        except User.DoesNotExist:
            raise serializers.ValidationError({
                'detail': 'No active account found with the given credentials.'
            })

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        # Add custom claims
        token['email'] = user.email
        token['role'] = user.role
        return token


class LocalizationSettingsSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = LocalizationSettings
        fields = (
            'id', 'owner', 'timezone', 'date_format', 'time_format', 'created_at', 'updated_at'
        )
        read_only_fields = ('id', 'owner', 'created_at', 'updated_at')
