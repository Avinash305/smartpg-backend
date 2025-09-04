from django.urls import path, include
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'localization-settings', views.LocalizationSettingsViewSet, basename='localization-settings')

urlpatterns = [
    # Authentication
    path('auth/token/', views.CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/register/', views.RegisterView.as_view(), name='register'),
    path('auth/verify-email/', views.VerifyEmailOTPView.as_view(), name='verify-email'),
    path('auth/resend-otp/', views.ResendEmailOTPView.as_view(), name='resend-otp'),
    path('auth/password/forgot/', views.PasswordResetRequestView.as_view(), name='password-forgot'),
    path('auth/password/verify-otp/', views.PasswordResetVerifyOTPView.as_view(), name='password-verify-otp'),
    
    # User management
    path('users/', views.UserListView.as_view(), name='user-list'),
    path('users/me/', views.CurrentUserView.as_view(), name='current-user'),
    path('users/<int:id>/', views.UserDetailView.as_view(), name='user-detail'),
    path('users/<int:id>/activities/', views.ActivityLogListView.as_view(), name='user-activities'),
    path('users/<int:id>/activities/<int:activity_id>/dismiss/', views.ActivityLogDismissView.as_view(), name='user-activity-dismiss'),

    # Global activity feed
    path('activities/', views.ActivityFeedListView.as_view(), name='activity-feed'),
]

urlpatterns += router.urls
