from django.urls import path
from .views import (
    PlansList,
    CurrentSubscriptionView,
    ChangePlanView,
    CancelSubscriptionView,
    ResumeSubscriptionView,
    CouponPreviewView,
    RazorpayCreateOrderView,
    RazorpayVerifyPaymentView,
)

urlpatterns = [
    path('plans/', PlansList.as_view(), name='subscription-plans'),
    path('current/', CurrentSubscriptionView.as_view(), name='subscription-current'),
    path('change-plan/', ChangePlanView.as_view(), name='subscription-change-plan'),
    path('cancel/', CancelSubscriptionView.as_view(), name='subscription-cancel'),
    path('resume/', ResumeSubscriptionView.as_view(), name='subscription-resume'),
    path('coupon/preview/', CouponPreviewView.as_view(), name='subscription-coupon-preview'),
    path('razorpay/create-order/', RazorpayCreateOrderView.as_view(), name='subscription-razorpay-create-order'),
    path('razorpay/verify/', RazorpayVerifyPaymentView.as_view(), name='subscription-razorpay-verify'),
]
