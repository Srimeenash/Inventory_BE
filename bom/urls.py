from rest_framework.routers import DefaultRouter
from .views import BOMViewSet, BOMItemViewSet

router = DefaultRouter()

router.register('bom', BOMViewSet, basename='bom')
router.register('bom-items', BOMItemViewSet, basename='bom-items')

urlpatterns = router.urls