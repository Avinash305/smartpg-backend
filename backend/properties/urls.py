from rest_framework.routers import DefaultRouter
from .views import BuildingViewSet, FloorViewSet, RoomViewSet, BedViewSet

router = DefaultRouter()
router.register(r'buildings', BuildingViewSet, basename='building')
router.register(r'floors', FloorViewSet, basename='floor')
router.register(r'rooms', RoomViewSet, basename='room')
router.register(r'beds', BedViewSet, basename='bed')

urlpatterns = router.urls
