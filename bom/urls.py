from rest_framework.routers import DefaultRouter
from .views import BOMViewSet

router = DefaultRouter()

router.register('bom', BOMViewSet, basename='bom')

urlpatterns = router.urls