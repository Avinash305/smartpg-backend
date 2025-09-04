from rest_framework import permissions
from django.db.models import Q

# ----- Staff JSON permission helpers -----
MODULES = {"buildings", "floors", "rooms", "beds", "tenants", "bookings", "payments", "invoices", "expenses"}
ACTIONS = {"view", "add", "edit", "delete"}

def _normalize_building_key(building_id):
    if building_id is None:
        return None
    try:
        return str(int(building_id))
    except Exception:
        return str(building_id)


def get_module_permission(user, module: str, action: str, building_id=None) -> bool:
    """
    Return whether a non-admin user has permission for a module/action scoped to a building.
    Admins (superuser/pg_admin) are handled by callers; this checks only the JSON map on the user.

    JSON shape on `User.permissions`:
    {
      "<building_id>": {
        "floors": {"view": true, "add": false, "edit": false, "delete": false},
        "rooms": {"view": true, ...},
        "beds": {"view": true, ...}
      },
      "global": {
        "floors": {"view": true, ...},
        ...
      }
    }

    Behavior:
    - If building_id is provided, check that building first, then fall back to global.
    - If building_id is None: check global first; if not explicitly boolean-True/False,
      then allow if the user has that action=True for the module in ANY building key.
    """
    if not user or not getattr(user, "permissions", None):
        return False
    module = str(module).lower()
    action = str(action).lower()
    if module not in MODULES or action not in ACTIONS:
        return False

    perms = user.permissions or {}
    bkey = _normalize_building_key(building_id)

    # Try building-scoped first
    if bkey and bkey in perms:
        mod_map = perms.get(bkey, {}).get(module, {})
        flag = mod_map.get(action)
        if isinstance(flag, bool):
            return flag
    # Fallback to global
    mod_map = perms.get("global", {}).get(module, {})
    flag = mod_map.get(action)
    if isinstance(flag, bool):
        return flag
    # New: if no building specified and global not explicitly set, allow if any building grants it
    if bkey is None:
        for k, v in perms.items():
            if k == "global":
                continue
            bmap = v.get(module, {})
            if isinstance(bmap.get(action), bool) and bmap.get(action):
                return True
    return False


def ensure_staff_module_permission(user, module: str, action: str, building_id=None) -> bool:
    """Convenience wrapper to check pg_staff permission only.
    Returns True for non-staff (callers can separately allow/deny admins)."""
    role = getattr(user, "role", None)
    if role != "pg_staff":
        return True
    return get_module_permission(user, module, action, building_id)


class IsAdminOrSelf(permissions.BasePermission):
    """
    Custom permission to only allow users to edit their own profile or admins to edit any profile.
    """
    def has_object_permission(self, request, view, obj):
        # Read permissions are allowed to any request,
        # so we'll always allow GET, HEAD or OPTIONS requests.
        if request.method in permissions.SAFE_METHODS:
            return True
            
        # Write permissions are only allowed to the owner of the profile or admin users.
        return obj == request.user or request.user.is_staff

class IsSuperUser(permissions.BasePermission):
    """
    Permission class that only allows superusers to have full access.
    """
    def has_permission(self, request, view):
        return request.user and request.user.is_superuser

    def has_object_permission(self, request, view, obj):
        return request.user and request.user.is_superuser

class IsPGAdminOrReadOnly(permissions.BasePermission):
    """
    Permission class that allows read-only access to all users
    and write access only to PG Admins.
    """
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user.is_authenticated and request.user.role == 'pg_admin'

class IsOwnerOrPGAdmin(permissions.BasePermission):
    """
    Permission class that allows:
    - Superusers: Full access to everything
    - PG Admins: Full access to their own data and their staff's data
    - Staff: Read/write access to their own data and ability to assign data
    """
    def has_permission(self, request, view):
        # Allow all authenticated users to read
        if request.method in permissions.SAFE_METHODS and request.user.is_authenticated:
            return True
            
        # Only allow authenticated users with specific roles to modify data
        return request.user.is_authenticated and (
            request.user.is_superuser or 
            request.user.role in ['pg_admin', 'pg_staff']
        )

    def has_object_permission(self, request, view, obj):
        # Superusers have full access to everything
        if request.user.is_superuser:
            return True
            
        # Allow read access to any authenticated user
        if request.method in permissions.SAFE_METHODS and request.user.is_authenticated:
            return True
            
        # For PG Admins
        if request.user.role == 'pg_admin':
            # Check if the object is a user model
            if hasattr(obj, 'role'):
                # Allow access if it's their own data or their staff's data
                return obj == request.user or obj.pg_admin == request.user
            # For other models, check if they own it or it belongs to their staff
            if hasattr(obj, 'pg_admin'):
                return obj.pg_admin == request.user
            if hasattr(obj, 'user'):
                return obj.user == request.user or (
                    hasattr(obj.user, 'pg_admin') and 
                    obj.user.pg_admin == request.user
                )
            return False
            
        # For Staff members
        if request.user.role == 'pg_staff':
            # Allow access to their own data
            if hasattr(obj, 'user'):
                return obj.user == request.user
            if hasattr(obj, 'assigned_to'):
                return obj.assigned_to == request.user
            return obj == request.user
            
        return False

class CanAssignData(permissions.BasePermission):
    """
    Permission class that allows staff members to assign data.
    """
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
            
        # Allow if user is superuser, pg_admin, or pg_staff
        return request.user.is_superuser or request.user.role in ['pg_admin', 'pg_staff']

    def has_object_permission(self, request, view, obj):
        # Superusers and PG Admins can assign anything
        if request.user.is_superuser or request.user.role == 'pg_admin':
            return True
            
        # Staff can only assign data they own
        if request.user.role == 'pg_staff':
            if hasattr(obj, 'assigned_by'):
                return obj.assigned_by == request.user
            return False
            
        return False
