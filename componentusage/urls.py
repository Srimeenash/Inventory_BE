from rest_framework.routers import DefaultRouter
from .views import ComponentUsageViewSet

router = DefaultRouter()
router.register(r'component-usage', ComponentUsageViewSet, basename='component-usage')

urlpatterns = router.urls