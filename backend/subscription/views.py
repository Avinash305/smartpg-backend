from django.utils import timezone
from django.db import transaction
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from decimal import Decimal
import logging
import razorpay
from razorpay.errors import SignatureVerificationError

from .models import SubscriptionPlan, Subscription, Coupon, CouponRedemption
from .serializers import SubscriptionPlanSerializer, SubscriptionSerializer
from .utils import compute_period_end, price_for_plan, get_coupon_by_code, validate_coupon_for, apply_discount, apply_gst, get_gst_percent
from django.apps import apps
logger = logging.getLogger(__name__)

def resolve_owner(user):
    """
    Resolve the PG Admin owner for the current user.
    - If user is pg_staff, return their pg_admin
    - Otherwise (pg_admin / superuser), return the user
    """
    role = getattr(user, 'role', None)
    if role == 'pg_staff' and getattr(user, 'pg_admin_id', None):
        return user.pg_admin
    return user

def _Building():
    # Lazy fetch to avoid circular import during app loading
    return apps.get_model('properties', 'Building')

class PlansList(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        plans = SubscriptionPlan.objects.filter(is_active=True).order_by('price_monthly', 'id')
        data = SubscriptionPlanSerializer(plans, many=True).data
        return Response(data)


class CurrentSubscriptionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        owner = resolve_owner(request.user)
        sub = (
            Subscription.objects
            .select_related('plan')
            .filter(owner=owner, is_current=True)
            .first()
        )
        # Backfill end date dynamically for older rows if missing
        if sub and not sub.current_period_end:
            try:
                sub.current_period_end = compute_period_end(sub.current_period_start, getattr(sub, 'billing_interval', '1m'))
                sub.save(update_fields=['current_period_end', 'updated_at'])
            except Exception:
                pass
        if not sub:
            # One-time default 1-month free subscription for first-time owners
            if not Subscription.objects.filter(owner=owner).exists():
                plan = SubscriptionPlan.objects.filter(is_active=True).order_by('price_monthly', 'id').first()
                if not plan:
                    return Response({'detail': 'No active plans available'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
                now = timezone.now()
                period_end = compute_period_end(now, '1m')
                # Prepare strict free-month limits
                base_limits = dict(plan.limits or {})
                free_limits = dict(base_limits)
                free_limits.update({
                    'buildings': 1,
                    'max_buildings': 1,
                    'staff': 1,
                    'max_staff': 1,
                    'floors': 5,
                    'max_floors': 5,
                    'rooms': 5,
                    'max_rooms': 5,
                    'beds': 5,
                    'max_beds': 5,
                    'tenants': 100,
                    'max_tenants': 100,
                })
                with transaction.atomic():
                    sub = Subscription.objects.create(
                        owner=owner,
                        plan=plan,
                        status='active',
                        billing_interval='1m',
                        current_period_start=now,
                        current_period_end=period_end,
                        trial_end=None,
                        cancel_at_period_end=False,
                        is_current=True,
                        meta={
                            'free_month': True,
                            'free_started_at': now.isoformat(),
                            'free_ends_at': period_end.isoformat(),
                            'features': dict(plan.features or {}),
                            'limits': free_limits,
                        },
                    )
            else:
                return Response({'detail': 'No current subscription'}, status=status.HTTP_404_NOT_FOUND)
        # Enforce only active subscriptions as valid "current"
        try:
            now = timezone.now()
            status_lc = (sub.status or '').lower()
            if status_lc != 'active':
                return Response({'detail': 'Subscription inactive'}, status=status.HTTP_404_NOT_FOUND)
            if sub.current_period_end and sub.current_period_end <= now:
                # Auto-mark expired and clear current flag
                try:
                    sub.status = 'expired'
                    sub.is_current = False
                    sub.save(update_fields=['status', 'is_current', 'updated_at'])
                except Exception:
                    pass
                return Response({'detail': 'Subscription expired'}, status=status.HTTP_404_NOT_FOUND)
        except Exception:
            # If anything goes wrong determining state, be safe and hide subscription
            return Response({'detail': 'Subscription unavailable'}, status=status.HTTP_404_NOT_FOUND)
        return Response(SubscriptionSerializer(sub).data)


class ChangePlanView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if getattr(request.user, 'role', None) == 'pg_staff':
            return Response({"detail": "Only PG Admin can change subscription"}, status=status.HTTP_403_FORBIDDEN)
        owner = resolve_owner(request.user)
        slug = request.data.get('plan_slug')
        plan_id = request.data.get('plan_id')
        interval = request.data.get('billing_interval')
        coupon_code = request.data.get('coupon_code')
        # Normalize potential legacy values to flexible codes
        if interval:
            iv = str(interval).strip().lower()
            if iv in {"monthly", "month", "1mo", "1month"}:
                interval = "1m"
            elif iv in {"yearly", "annual", "annually", "12mo", "12month", "12months"}:
                interval = "12m"
            else:
                interval = iv
        if not slug and not plan_id:
            return Response({"detail": "plan_slug or plan_id required"}, status=status.HTTP_400_BAD_REQUEST)

        plan_qs = SubscriptionPlan.objects.filter(is_active=True)
        plan = plan_qs.filter(slug=slug).first() if slug else plan_qs.filter(id=plan_id).first()
        if not plan:
            return Response({"detail": "Plan not found"}, status=status.HTTP_404_NOT_FOUND)

        allowed = list(plan.available_intervals or ["1m", "3m", "6m", "12m"])
        if interval and interval not in allowed:
            return Response({"detail": "Billing interval not available for this plan"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            sub = (
                Subscription.objects
                .select_for_update()
                .filter(owner=owner, is_current=True)
                .first()
            )
            if not sub:
                return Response({"detail": "No current subscription"}, status=status.HTTP_404_NOT_FOUND)
            # Enforce building count against target plan limit before changing plan
            used_active = _Building().objects.filter(owner=owner, is_active=True).count()
            try:
                target_limit = int((plan.limits or {}).get('max_buildings'))
            except Exception:
                target_limit = None
            if target_limit is not None and used_active > target_limit:
                return Response({
                    "detail": f"Your active buildings ({used_active}) exceed the selected plan's limit ({target_limit}). Deactivate buildings or choose a higher plan."
                }, status=status.HTTP_400_BAD_REQUEST)
            now = timezone.now()
            # Decide chosen interval first
            chosen = None
            if not sub:
                chosen = interval or ("1m" if "1m" in allowed else allowed[0])
            else:
                chosen = interval or (sub.billing_interval if getattr(sub, 'billing_interval', None) in allowed else ("1m" if "1m" in allowed else allowed[0]))

            # Compute base price
            price = price_for_plan(plan, chosen)
            base_amount, currency = (Decimal('0.00'), plan.currency or 'INR') if not price else (price[0], price[1])

            applied = None
            coupon = None
            if coupon_code:
                coupon = get_coupon_by_code(coupon_code)
                # Validate coupon applicability and usage limits
                validate_coupon_for(owner, plan, chosen, coupon)
                final_amt, discount_amt = apply_discount(base_amount, currency, coupon)
                applied = {
                    'code': coupon.code,
                    'discount_type': coupon.discount_type,
                    'value': str(coupon.value),
                    'currency': currency,
                    'base_amount': str(base_amount),
                    'discount_amount': str(discount_amt),
                    'final_amount': str(final_amt),
                    'applied_at': now.isoformat(),
                }

            # Update existing current subscription (no auto-creation)
            sub.plan = plan
            sub.billing_interval = chosen
            sub.status = 'active'
            sub.cancel_at_period_end = False
            sub.current_period_start = now
            sub.current_period_end = compute_period_end(now, chosen)
            sub.trial_end = None
            meta = sub.meta or {}
            if applied:
                meta['applied_coupon'] = applied
            else:
                meta.pop('applied_coupon', None)
            for k in ('trial_days', 'features', 'limits', 'free_month', 'free_started_at', 'free_ends_at'):
                if k in meta:
                    meta.pop(k, None)
            sub.meta = meta
            sub.save(update_fields=['plan', 'billing_interval', 'status', 'cancel_at_period_end', 'current_period_start', 'current_period_end', 'trial_end', 'meta', 'updated_at'])

        return Response(SubscriptionSerializer(sub).data)


class CancelSubscriptionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if getattr(request.user, 'role', None) == 'pg_staff':
            return Response({"detail": "Only PG Admin can cancel subscription"}, status=status.HTTP_403_FORBIDDEN)
        owner = resolve_owner(request.user)
        with transaction.atomic():
            sub = (
                Subscription.objects
                .select_for_update()
                .filter(owner=owner, is_current=True)
                .first()
            )
            if not sub:
                return Response({"detail": "No current subscription"}, status=status.HTTP_404_NOT_FOUND)
            sub.cancel_at_period_end = True
            sub.save(update_fields=['cancel_at_period_end', 'updated_at'])
        return Response({"status": "scheduled_to_cancel"})


class ResumeSubscriptionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if getattr(request.user, 'role', None) == 'pg_staff':
            return Response({"detail": "Only PG Admin can resume subscription"}, status=status.HTTP_403_FORBIDDEN)
        owner = resolve_owner(request.user)
        with transaction.atomic():
            sub = (
                Subscription.objects
                .select_for_update()
                .filter(owner=owner, is_current=True)
                .first()
            )
            if not sub:
                return Response({"detail": "No current subscription"}, status=status.HTTP_404_NOT_FOUND)
            sub.cancel_at_period_end = False
            if sub.status in ("canceled", "expired"):
                sub.status = "active"
            sub.save(update_fields=['cancel_at_period_end', 'status', 'updated_at'])
        return Response(SubscriptionSerializer(sub).data)


class CouponPreviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        owner = resolve_owner(request.user)
        slug = request.data.get('plan_slug')
        plan_id = request.data.get('plan_id')
        interval = request.data.get('billing_interval')
        coupon_code = request.data.get('coupon_code')

        if not coupon_code:
            return Response({'detail': 'coupon_code required'}, status=status.HTTP_400_BAD_REQUEST)

        # Normalize interval
        if interval:
            iv = str(interval).strip().lower()
            if iv in {"monthly", "month", "1mo", "1month"}:
                interval = "1m"
            elif iv in {"yearly", "annual", "annually", "12mo", "12month", "12months"}:
                interval = "12m"
            else:
                interval = iv

        plan_qs = SubscriptionPlan.objects.filter(is_active=True)
        plan = plan_qs.filter(slug=slug).first() if slug else plan_qs.filter(id=plan_id).first()
        if not plan:
            return Response({"detail": "Plan not found"}, status=status.HTTP_404_NOT_FOUND)

        allowed = list(plan.available_intervals or ["1m", "3m", "6m", "12m"])
        chosen = interval or ("1m" if "1m" in allowed else allowed[0])
        if chosen not in allowed:
            return Response({"detail": "Billing interval not available for this plan"}, status=status.HTTP_400_BAD_REQUEST)

        coupon = get_coupon_by_code(coupon_code)
        if not coupon:
            return Response({"detail": "Coupon not found"}, status=status.HTTP_404_NOT_FOUND)

        # Validate coupon applicability
        validate_coupon_for(owner, plan, chosen, coupon)

        price = price_for_plan(plan, chosen)
        base_amount, currency = (Decimal('0.00'), plan.currency or 'INR') if not price else (price[0], price[1])
        final_amt, discount_amt = apply_discount(base_amount, currency, coupon)
        gross_amt, gst_amt = apply_gst(final_amt)
        gst_percent = str(get_gst_percent())

        return Response({
            'plan': plan.slug,
            'interval': chosen,
            'currency': currency,
            'base_amount': str(base_amount),
            'discount_amount': str(discount_amt),
            'final_amount': str(final_amt),
            'gst_amount': str(gst_amt),
            'gross_amount': str(gross_amt),
            'gst_percent': gst_percent,
            'coupon': {
                'code': coupon.code,
                'discount_type': coupon.discount_type,
                'value': str(coupon.value),
                'valid_from': coupon.valid_from,
                'valid_until': coupon.valid_until,
            }
        })


class RazorpayCreateOrderView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Only PG Admin can initiate subscription payments
        if getattr(request.user, 'role', None) == 'pg_staff':
            return Response({"detail": "Only PG Admin can create subscription payment order"}, status=status.HTTP_403_FORBIDDEN)

        key_id = getattr(settings, 'RAZORPAY_KEY_ID', None)
        key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', None)
        if not key_id or not key_secret:
            return Response({"detail": "Razorpay is not configured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        owner = resolve_owner(request.user)
        slug = request.data.get('plan_slug')
        plan_id = request.data.get('plan_id')
        interval = request.data.get('billing_interval')
        coupon_code = request.data.get('coupon_code')

        # Normalize interval
        if interval:
            iv = str(interval).strip().lower()
            if iv in {"monthly", "month", "1mo", "1month"}:
                interval = "1m"
            elif iv in {"yearly", "annual", "annually", "12mo", "12month", "12months"}:
                interval = "12m"
            else:
                interval = iv

        plan_qs = SubscriptionPlan.objects.filter(is_active=True)
        plan = plan_qs.filter(slug=slug).first() if slug else plan_qs.filter(id=plan_id).first()
        if not plan:
            return Response({"detail": "Plan not found"}, status=status.HTTP_404_NOT_FOUND)

        allowed = list(plan.available_intervals or ["1m", "3m", "6m", "12m"])
        chosen = interval or ("1m" if "1m" in allowed else allowed[0])
        if chosen not in allowed:
            return Response({"detail": "Billing interval not available for this plan"}, status=status.HTTP_400_BAD_REQUEST)

        # Require existing current subscription (no auto-create)
        existing_sub = Subscription.objects.filter(owner=owner, is_current=True).first()
        if not existing_sub:
            return Response({"detail": "No current subscription"}, status=status.HTTP_404_NOT_FOUND)

        # Block payment initiation during active free month
        try:
            if isinstance(existing_sub.meta, dict) and existing_sub.meta.get('free_month'):
                now_chk = timezone.now()
                if existing_sub.current_period_end and existing_sub.current_period_end > now_chk and (existing_sub.status or '').lower() == 'active':
                    return Response({
                        "detail": "Payment is disabled during your one-month free period. You can upgrade once the free period ends."
                    }, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            pass

        # Preflight: block orders for plans below current active building count
        used_active = _Building().objects.filter(owner=owner, is_active=True).count()
        try:
            target_limit = int((plan.limits or {}).get('max_buildings'))
        except Exception:
            target_limit = None
        if target_limit is not None and used_active > target_limit:
            return Response({
                "detail": f"Your active buildings ({used_active}) exceed the selected plan's limit ({target_limit}). Deactivate buildings or choose a higher plan."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Compute base/final amount
        price = price_for_plan(plan, chosen)
        base_amount, currency = (Decimal('0.00'), plan.currency or 'INR') if not price else (price[0], price[1])

        applied = None
        final_amt = base_amount
        discount_amt = Decimal('0.00')
        coupon = None
        if coupon_code:
            coupon = get_coupon_by_code(coupon_code)
            validate_coupon_for(owner, plan, chosen, coupon)
            final_amt, discount_amt = apply_discount(base_amount, currency, coupon)
            now = timezone.now()
            applied = {
                'code': coupon.code,
                'discount_type': coupon.discount_type,
                'value': str(coupon.value),
                'currency': currency,
                'base_amount': str(base_amount),
                'discount_amount': str(discount_amt),
                'final_amount': str(final_amt),
                'applied_at': now.isoformat(),
            }

        # Apply GST on the net amount (after discounts)
        gross_amt, gst_amt = apply_gst(final_amt)
        gst_percent = str(get_gst_percent())

        # Amount in smallest unit (paise)
        try:
            amount_paise = int((gross_amt.quantize(Decimal('0.01')) * Decimal('100')).to_integral_value())
        except Exception:
            return Response({"detail": "Invalid amount for payment"}, status=status.HTTP_400_BAD_REQUEST)
        if amount_paise <= 0:
            return Response({"detail": "Amount must be greater than zero"}, status=status.HTTP_400_BAD_REQUEST)


        # Create Razorpay order
        client = razorpay.Client(auth=(key_id, key_secret))
        receipt = f"sub-{owner.id}-{plan.id}-{int(timezone.now().timestamp())}"
        notes = {
            'owner_id': str(owner.id),
            'plan_id': str(plan.id),
            'plan_slug': str(plan.slug),
            'interval': str(chosen),
            'coupon_code': str(coupon.code if coupon else ''),
            'base_amount': str(base_amount),
            'final_amount': str(final_amt),
            'gst_amount': str(gst_amt),
            'gross_amount': str(gross_amt),
            'gst_percent': gst_percent,
            'currency': str(currency),
        }
        try:
            order = client.order.create({
                'amount': amount_paise,
                'currency': currency,
                'receipt': receipt,
                'payment_capture': 1,
                'notes': notes,
            })
        except Exception as e:
            logger.exception("Failed to create Razorpay order: %s", str(e))
            return Response({"detail": "Failed to create payment order"}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({
            'order_id': order.get('id'),
            'amount': order.get('amount'),  # in paise
            'currency': order.get('currency'),
            'status': order.get('status'),
            'key_id': key_id,
            'notes': order.get('notes', {}),
            'applied_coupon': applied,
            'gst_amount': str(gst_amt),
            'gross_amount': str(gross_amt),
            'gst_percent': gst_percent,
        })


class RazorpayVerifyPaymentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if getattr(request.user, 'role', None) == 'pg_staff':
            return Response({"detail": "Only PG Admin can verify subscription payment"}, status=status.HTTP_403_FORBIDDEN)

        key_id = getattr(settings, 'RAZORPAY_KEY_ID', None)
        key_secret = getattr(settings, 'RAZORPAY_KEY_SECRET', None)
        if not key_id or not key_secret:
            return Response({"detail": "Razorpay is not configured"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        order_id = request.data.get('razorpay_order_id')
        payment_id = request.data.get('razorpay_payment_id')
        signature = request.data.get('razorpay_signature')
        if not order_id or not payment_id or not signature:
            return Response({"detail": "Missing payment verification parameters"}, status=status.HTTP_400_BAD_REQUEST)

        client = razorpay.Client(auth=(key_id, key_secret))
        # Verify signature
        try:
            client.utility.verify_payment_signature({
                'razorpay_order_id': order_id,
                'razorpay_payment_id': payment_id,
                'razorpay_signature': signature,
            })
        except SignatureVerificationError:
            return Response({"detail": "Invalid payment signature"}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.exception("Payment signature verification failed: %s", str(e))
            return Response({"detail": "Payment verification error"}, status=status.HTTP_400_BAD_REQUEST)

        # Fetch order to retrieve notes and amount
        try:
            order = client.order.fetch(order_id)
        except Exception as e:
            logger.exception("Failed to fetch Razorpay order %s: %s", order_id, str(e))
            return Response({"detail": "Unable to fetch payment order"}, status=status.HTTP_400_BAD_REQUEST)

        notes = order.get('notes') or {}
        plan_slug = notes.get('plan_slug')
        plan_id_note = notes.get('plan_id')
        interval = notes.get('interval')
        coupon_code = notes.get('coupon_code') or None
        currency = notes.get('currency') or 'INR'
        try:
            expected_amount_paise = int(order.get('amount') or 0)
        except Exception:
            expected_amount_paise = 0

        # Resolve plan
        plan_qs = SubscriptionPlan.objects.filter(is_active=True)
        plan = plan_qs.filter(slug=plan_slug).first() if plan_slug else None
        if not plan and plan_id_note:
            try:
                plan = plan_qs.filter(id=int(plan_id_note)).first()
            except Exception:
                plan = None
        if not plan:
            return Response({"detail": "Plan not found"}, status=status.HTTP_404_NOT_FOUND)

        allowed = list(plan.available_intervals or ["1m", "3m", "6m", "12m"])
        chosen = (interval or '').strip().lower()
        if chosen in {"monthly", "month", "1mo", "1month"}:
            chosen = "1m"
        elif chosen in {"yearly", "annual", "annually", "12mo", "12month", "12months"}:
            chosen = "12m"
        if chosen not in allowed:
            return Response({"detail": "Billing interval not available for this plan"}, status=status.HTTP_400_BAD_REQUEST)

        # Enforce building count against target plan before updating subscription
        owner = resolve_owner(request.user)
        used_active = _Building().objects.filter(owner=owner, is_active=True).count()
        try:
            target_limit = int((plan.limits or {}).get('max_buildings'))
        except Exception:
            target_limit = None
        if target_limit is not None and used_active > target_limit:
            return Response({
                "detail": f"Your active buildings ({used_active}) exceed the selected plan's limit ({target_limit}). Deactivate buildings or choose a higher plan."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Recompute amount and validate matches with order amount
        price = price_for_plan(plan, chosen)
        base_amount, curr = (Decimal('0.00'), plan.currency or 'INR') if not price else (price[0], price[1])
        if (curr or '').upper() != (currency or '').upper():
            return Response({"detail": "Currency mismatch"}, status=status.HTTP_400_BAD_REQUEST)

        final_amt = base_amount
        discount_amt = Decimal('0.00')
        coupon = None
        if coupon_code:
            coupon = get_coupon_by_code(coupon_code)
            validate_coupon_for(owner, plan, chosen, coupon)
            final_amt, discount_amt = apply_discount(base_amount, curr, coupon)

        # Apply GST after discounts
        gross_amt, gst_amt = apply_gst(final_amt)
        gst_percent = str(get_gst_percent())

        try:
            calc_amount_paise = int((gross_amt.quantize(Decimal('0.01')) * Decimal('100')).to_integral_value())
        except Exception:
            return Response({"detail": "Invalid amount computation"}, status=status.HTTP_400_BAD_REQUEST)
        if calc_amount_paise != expected_amount_paise:
            return Response({"detail": "Payment amount mismatch"}, status=status.HTTP_400_BAD_REQUEST)

        # Update subscription atomically
        with transaction.atomic():
            sub = (
                Subscription.objects
                .select_for_update()
                .filter(owner=owner, is_current=True)
                .first()
            )
            if not sub:
                return Response({"detail": "No current subscription"}, status=status.HTTP_404_NOT_FOUND)

            # Idempotency: if payment already recorded on current subscription, return it as-is
            if isinstance(sub.meta, dict) and sub.meta.get('rzp_payment_id') == payment_id:
                return Response(SubscriptionSerializer(sub).data)

            now = timezone.now()
            applied = None
            if coupon:
                applied = {
                    'code': coupon.code,
                    'discount_type': coupon.discount_type,
                    'value': str(coupon.value),
                    'currency': curr,
                    'base_amount': str(base_amount),
                    'discount_amount': str(discount_amt),
                    'final_amount': str(final_amt),
                    'applied_at': now.isoformat(),
                }

            # Update existing current subscription
            sub.plan = plan
            sub.billing_interval = chosen
            sub.status = 'active'
            sub.cancel_at_period_end = False
            sub.current_period_start = now
            sub.current_period_end = compute_period_end(now, chosen)
            sub.trial_end = None
            meta = sub.meta or {}
            meta['rzp_order_id'] = order_id
            meta['rzp_payment_id'] = payment_id
            # Persist pricing breakdown
            meta['currency'] = curr
            meta['base_amount'] = str(base_amount)
            meta['final_amount'] = str(final_amt)
            meta['gst_amount'] = str(gst_amt)
            meta['gross_amount'] = str(gross_amt)
            meta['gst_percent'] = gst_percent
            if applied:
                meta['applied_coupon'] = applied
            else:
                meta.pop('applied_coupon', None)
            for k in ('trial_days', 'features', 'limits', 'free_month', 'free_started_at', 'free_ends_at'):
                if k in meta:
                    meta.pop(k, None)
            sub.meta = meta
            sub.save(update_fields=['plan', 'billing_interval', 'status', 'cancel_at_period_end', 'current_period_start', 'current_period_end', 'trial_end', 'meta', 'updated_at'])

            # Record coupon redemption (best-effort)
            if coupon:
                try:
                    CouponRedemption.objects.create(coupon=coupon, owner=owner, subscription=sub)
                except Exception as e:
                    logger.warning(
                        "Failed to record coupon redemption for code=%s owner_id=%s subscription_id=%s: %s",
                        getattr(coupon, 'code', None), getattr(owner, 'id', None), getattr(sub, 'id', None), str(e), exc_info=True,
                    )

        return Response(SubscriptionSerializer(sub).data)


class StartTrialView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Trials are discontinued
        return Response({"detail": "Trial is no longer available"}, status=status.HTTP_404_NOT_FOUND)
