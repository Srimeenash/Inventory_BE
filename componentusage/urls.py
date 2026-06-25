from rest_framework.routers import DefaultRouter
from .views import ComponentUsageViewSet

router = DefaultRouter()
router.register(r"", ComponentUsageViewSet)  # 👈 EMPTY STRING FIX

urlpatterns = router.urls