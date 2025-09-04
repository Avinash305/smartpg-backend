import json
from datetime import timedelta
import threading
from typing import Optional, Tuple
from django.utils.deprecation import MiddlewareMixin
from django.utils import timezone
from django.apps import apps
from django.contrib.auth.models import AnonymousUser
try:
    # SimpleJWT is used for API auth; we'll fallback to it when request.user is anonymous
    from rest_framework_simplejwt.authentication import JWTAuthentication
except Exception:  # pragma: no cover
    JWTAuthentication = None

"""
Avoid importing app models at module import time to prevent circular imports.
Use lazy getters via apps.get_model when needed.
"""

def _ActivityLog():
    try:
        return apps.get_model('accounts', 'ActivityLog')
    except Exception:
        return None

def _log_activity(user, action, description=None, meta=None):
    try:
        from .models import log_activity as _log
        return _log(user, action, description=description, meta=meta)
    except Exception:
        return None

def _Building():
    try:
        return apps.get_model('properties', 'Building')
    except Exception:
        return None

def _Floor():
    try:
        return apps.get_model('properties', 'Floor')
    except Exception:
        return None

def _Room():
    try:
        return apps.get_model('properties', 'Room')
    except Exception:
        return None

def _Bed():
    try:
        return apps.get_model('properties', 'Bed')
    except Exception:
        return None


_thread_locals = threading.local()


def get_current_user() -> Optional['User']:
    return getattr(_thread_locals, 'user', None)


class RequestUserMiddleware(MiddlewareMixin):
    def process_request(self, request):
        _thread_locals.user = getattr(request, 'user', None)


class ActivityLogMiddleware(MiddlewareMixin):
    METHOD_ACTION = {
        'POST': 'create',
        'PUT': 'update',
        'PATCH': 'update',
        'DELETE': 'delete',
    }

    TRACKED_ENTITIES = {'buildings', 'floors', 'rooms', 'beds'}

    def _parse_entity_and_id(self, path: str) -> Tuple[Optional[str], Optional[int]]:
        try:
            parts = [p for p in path.split('/') if p]
            for i, p in enumerate(parts):
                if p.isdigit() and i > 0:
                    return parts[i-1], int(p)
            return (parts[-1] if parts else None, None)
        except Exception:
            return (None, None)

    def _collect_pre_state(self, entity: str, obj_id: int) -> dict:
        try:
            if entity == 'beds' and _Bed() is not None:
                Bed = _Bed()
                b = Bed.objects.select_related('room__floor__building').filter(pk=obj_id).first()
                if not b:
                    return {}
                return {
                    'number': b.number,
                    'status': b.status,
                    'notes': b.notes or '',
                    'room_number': b.room.number,
                    'floor_display': b.room.floor.get_number_display(),
                    'building_name': b.room.floor.building.name,
                }
            if entity == 'rooms' and _Room() is not None:
                Room = _Room()
                r = Room.objects.select_related('floor__building').filter(pk=obj_id).first()
                if not r:
                    return {}
                return {
                    'number': r.number,
                    'notes': r.notes or '',
                    'floor_display': r.floor.get_number_display(),
                    'building_name': r.floor.building.name,
                }
            if entity == 'floors' and _Floor() is not None:
                Floor = _Floor()
                f = Floor.objects.select_related('building').filter(pk=obj_id).first()
                if not f:
                    return {}
                return {
                    'number': f.number,
                    'notes': f.notes or '',
                    'building_name': f.building.name,
                    'floor_display': f.get_number_display(),
                }
            if entity == 'buildings' and _Building() is not None:
                Building = _Building()
                b = Building.objects.filter(pk=obj_id).first()
                if not b:
                    return {}
                return {
                    'name': b.name,
                    'notes': b.notes or '',
                }
        except Exception:
            return {}
        return {}

    def process_view(self, request, view_func, view_args, view_kwargs):
        # Capture pre-state for updates to compute old→new
        try:
            method = request.method.upper()
            if method in ('PUT', 'PATCH'):
                entity, obj_id = self._parse_entity_and_id(request.path)
                if entity in self.TRACKED_ENTITIES and obj_id:
                    setattr(request, '_activity_pre_state', self._collect_pre_state(entity, obj_id))
        except Exception:
            pass
        return None

    def process_response(self, request, response):
        try:
            # Per-request guard: if we've already logged, skip
            if getattr(request, '_activity_log_recorded', False):
                return response
            user = getattr(request, 'user', None)
            method = request.method.upper()
            action = self.METHOD_ACTION.get(method)
            # If user is not authenticated on the Django request (common with JWT), try authenticating via SimpleJWT
            if (not getattr(user, 'is_authenticated', False)) and JWTAuthentication is not None:
                try:
                    authenticator = JWTAuthentication()
                    auth_result = authenticator.authenticate(request)
                    if auth_result:
                        user, _ = auth_result
                except Exception:
                    pass

            if user and getattr(user, 'is_authenticated', False) and action:
                # Only log success responses (2xx)
                status = getattr(response, 'status_code', 0)
                if 200 <= status < 300:
                    # Try to extract object details from response data if available
                    obj_id = None
                    obj_name = None
                    try:
                        data = getattr(response, 'data', None)
                        if isinstance(data, dict):
                            obj_id = data.get('id') or data.get('pk')
                            # Generic quick pick (may be overridden below for entity-specific display)
                            for key in ('name', 'title', 'full_name', 'label', 'room_name', 'building_name'):
                                if key in data and data.get(key):
                                    obj_name = str(data.get(key))
                                    break
                    except Exception:
                        data = None
                    # derive entity from path e.g., /api/properties/floors/1/
                    path = request.path
                    entity = None
                    try:
                        parts = [p for p in path.split('/') if p]
                        # crude heuristic: pick plural segment before numeric id
                        for i, p in enumerate(parts):
                            if p.isdigit() and i > 0:
                                entity = parts[i-1]
                                break
                        if not entity and parts:
                            entity = parts[-1]
                    except Exception:
                        entity = None

                    # Entity-specific rich name (override generic when available)
                    try:
                        if isinstance(data, dict):
                            if entity == 'floors':
                                num = data.get('number')
                                bname = data.get('building_name')
                                rich = None
                                if num is not None and bname:
                                    rich = f"Floor {num} - {bname}"
                                elif num is not None:
                                    rich = f"Floor {num}"
                                if rich:
                                    obj_name = rich
                            elif entity == 'rooms':
                                rnum = data.get('number')
                                fdisp = data.get('floor_display') or data.get('floor')
                                rich = None
                                if rnum is not None and fdisp:
                                    rich = f"Room {rnum} - {fdisp}"
                                elif rnum is not None:
                                    rich = f"Room {rnum}"
                                if rich:
                                    obj_name = rich
                            elif entity == 'beds':
                                bnum = data.get('number')
                                rnum = data.get('room_number') or data.get('room')
                                fdisp = data.get('floor_display') or data.get('floor')
                                bldg = data.get('building_name')
                                parts = []
                                if bnum is not None:
                                    parts.append(f"Bed {bnum}")
                                if rnum is not None:
                                    parts.append(f"Room {rnum}")
                                if fdisp:
                                    parts.append(str(fdisp))
                                if bldg:
                                    parts.append(str(bldg))
                                if parts:
                                    obj_name = " - ".join(parts)
                    except Exception:
                        pass

                    # Final fallback: DB lookups if still missing
                    try:
                        if not obj_name and obj_id:
                            if entity == 'buildings' and _Building() is not None:
                                Building = _Building()
                                b = Building.objects.filter(pk=obj_id).only('name').first()
                                if b:
                                    obj_name = str(b.name)
                            elif entity == 'floors' and _Floor() is not None:
                                Floor = _Floor()
                                f = Floor.objects.select_related('building').filter(pk=obj_id).first()
                                if f:
                                    obj_name = f"{f.get_number_display()} - {f.building.name}"
                            elif entity == 'rooms' and _Room() is not None:
                                Room = _Room()
                                r = Room.objects.select_related('floor__building').filter(pk=obj_id).first()
                                if r:
                                    obj_name = f"Room {r.number} - {r.floor.get_number_display()}"
                            elif entity == 'beds' and _Bed() is not None:
                                Bed = _Bed()
                                b = Bed.objects.select_related('room__floor__building').filter(pk=obj_id).first()
                                if b:
                                    obj_name = f"Bed {b.number} - Room {b.room.number} - {b.room.floor.get_number_display()} - {b.room.floor.building.name}"
                    except Exception:
                        pass

                    # Pre/post change capture
                    changes = {}
                    change_summary = None
                    change_title = None
                    change_details = None
                    pre_state = getattr(request, '_activity_pre_state', {}) if method in ('PUT', 'PATCH') else {}
                    try:
                        content_type = request.META.get('CONTENT_TYPE', '')
                        raw = None
                        if 'application/json' in content_type:
                            raw = request.body.decode('utf-8') if hasattr(request, 'body') else None
                            if raw:
                                body = json.loads(raw)
                                if isinstance(body, dict):
                                    resp_dict = data if isinstance(data, dict) else {}
                                    # Build result-focused changes (prefer response values)
                                    for k, v in body.items():
                                        if k in {'password'}:
                                            continue
                                        new_v = resp_dict.get(k, v)
                                        changes[k] = new_v
                                    # Normalize certain displays
                                    if entity == 'beds' and resp_dict:
                                        if 'room' in changes:
                                            rn = resp_dict.get('room_number')
                                            if rn is not None:
                                                changes['room'] = f"Room {rn}"
                                        if 'status' in changes and resp_dict.get('status'):
                                            changes['status'] = resp_dict['status']
                                    if entity == 'rooms' and resp_dict and 'number' in changes:
                                        changes['number'] = resp_dict.get('number')
                                    if entity == 'floors' and resp_dict and 'number' in changes:
                                        changes['number'] = resp_dict.get('number')
                                    # Compute old→new where possible
                                    diffs = {}
                                    for k, new_v in changes.items():
                                        old_v = pre_state.get(k)
                                        if old_v is None:
                                            # For beds/rooms allow some aliasing
                                            if k == 'room' and 'room_number' in pre_state:
                                                old_v = f"Room {pre_state.get('room_number')}"
                                            if k == 'number' and pre_state.get('number') is not None:
                                                old_v = pre_state.get('number')
                                        if method in ('PUT', 'PATCH'):
                                            if old_v is None or old_v == new_v:
                                                continue
                                            diffs[k] = {'from': old_v, 'to': new_v}
                                    # Title/details heuristics
                                    if entity == 'beds' and 'status' in diffs:
                                        change_title = 'Changed Bed Status'
                                        short = obj_name or 'Bed'
                                        old = diffs['status']['from']
                                        new = diffs['status']['to']
                                        # Compact object description in parens
                                        obj_paren = ''
                                        rn = (data or {}).get('room_number') or pre_state.get('room_number')
                                        fd = (data or {}).get('floor_display') or pre_state.get('floor_display')
                                        bn = (data or {}).get('building_name') or pre_state.get('building_name')
                                        parts = []
                                        if rn: parts.append(f"Room {rn}")
                                        if fd: parts.append(fd)
                                        if bn and entity != 'buildings': parts.append(bn)
                                        obj_paren = f" ({', '.join(parts)})" if parts else ''
                                        bed_num = (data or {}).get('number') or pre_state.get('number')
                                        label = f"Bed {bed_num}" if bed_num else short
                                        change_details = f"{label}{obj_paren} {old} → {new}"
                                    elif entity == 'rooms' and ('number' in diffs or 'notes' in diffs):
                                        change_title = 'Updated Room'
                                    elif entity == 'floors' and 'number' in diffs:
                                        change_title = 'Updated Floor'
                                    elif 'notes' in changes and (pre_state.get('notes', '') or '') == '' and changes.get('notes') and method == 'POST':
                                        change_title = 'Added Note'
                                        quote = changes.get('notes')
                                        # Build context line
                                        rn = (data or {}).get('room_number') or pre_state.get('room_number')
                                        fd = (data or {}).get('floor_display') or pre_state.get('floor_display')
                                        bn = (data or {}).get('building_name') or pre_state.get('building_name')
                                        label = obj_name or ''
                                        ctx_parts = []
                                        if rn: ctx_parts.append(f"Room {rn}")
                                        if bn: ctx_parts.append(bn)
                                        if fd and 'Room' not in (ctx_parts[0] if ctx_parts else ''):
                                            ctx_parts.append(fd)
                                        context_line = ", ".join(ctx_parts) or label
                                        change_details = f"{context_line}\n\"{quote}\""
                                    # Build generic summary list
                                    if not change_title and changes:
                                        change_title = f"{'Created' if action=='create' else 'Updated'} {entity[:-1].capitalize() if entity else 'Item'}"
                                    # Build change_summary string (even when diffs missing)
                                    if changes:
                                        parts = []
                                        for k, v in changes.items():
                                            try:
                                                sval = v if isinstance(v, (str, int, float)) else json.dumps(v, ensure_ascii=False)
                                            except Exception:
                                                sval = str(v)
                                            parts.append(f"{k}: {sval}")
                                        change_summary = ", ".join(parts)[:300]
                                    # Always craft detailed per-field lines if not already provided
                                    if not change_details:
                                        lines = []
                                        if method in ('PUT', 'PATCH') and diffs:
                                            # Old → New lines
                                            for k, d in diffs.items():
                                                old = d.get('from')
                                                new = d.get('to')
                                                try:
                                                    old_s = old if isinstance(old, (str, int, float)) else json.dumps(old, ensure_ascii=False)
                                                except Exception:
                                                    old_s = str(old)
                                                try:
                                                    new_s = new if isinstance(new, (str, int, float)) else json.dumps(new, ensure_ascii=False)
                                                except Exception:
                                                    new_s = str(new)
                                                lines.append(f"{k}: {old_s} → {new_s}")
                                        elif method == 'POST' and changes:
                                            # Added fields list
                                            for k, v in changes.items():
                                                try:
                                                    vs = v if isinstance(v, (str, int, float)) else json.dumps(v, ensure_ascii=False)
                                                except Exception:
                                                    vs = str(v)
                                                lines.append(f"{k}: {vs}")
                                        if lines:
                                            change_details = "\n".join(lines)[:800]
                    except Exception:
                        pass

                    # Compose description for UI fallback
                    description = None
                    if change_title and change_summary:
                        description = f"{change_title} — {change_summary}"
                    else:
                        description = f"{method} {path}"

                    meta = {
                        'status': status,
                        'path': path,
                        'method': method,
                        'entity': entity,
                        'object_id': obj_id,
                        'object_name': obj_name,
                    }
                    if changes:
                        meta['changes'] = changes
                    if change_summary:
                        meta['change_summary'] = change_summary
                    if change_title:
                        meta['change_title'] = change_title
                    if change_details:
                        meta['change_details'] = change_details

                    # Short-window DB dedupe: avoid duplicates within 2 seconds with same signature
                    try:
                        window_start = timezone.now() - timedelta(seconds=2)
                        AL = _ActivityLog()
                        exists = False
                        if AL is not None:
                            exists = AL.objects.filter(
                                user=user,
                                action=action,
                                description=description,
                                timestamp__gte=window_start,
                            ).exists()
                        if not exists:
                            _log_activity(user, action, description=description, meta=meta)
                            # Mark this request as logged
                            setattr(request, '_activity_log_recorded', True)
                    except Exception:
                        # Fallback: even if dedupe check fails, ensure we don't log twice per request
                        if not getattr(request, '_activity_log_recorded', False):
                            _log_activity(user, action, description=description, meta=meta)
                            setattr(request, '_activity_log_recorded', True)
        except Exception:
            # Never break request flow due to logging errors
            pass
        return response