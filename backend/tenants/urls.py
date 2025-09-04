from rest_framework.routers import DefaultRouter
from .views import (
    TenantViewSet,
    EmergencyContactViewSet,
    StayViewSet,
    BedHistoryViewSet,
)

router = DefaultRouter()
router.register(r'tenants', TenantViewSet, basename='tenant')
router.register(r'contacts', EmergencyContactViewSet, basename='emergency-contact')
router.register(r'stays', StayViewSet, basename='stay')
router.register(r'bed-history', BedHistoryViewSet, basename='bed-history')

urlpatterns = router.urls
