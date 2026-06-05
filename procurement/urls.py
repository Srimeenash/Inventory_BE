from rest_framework.routers import DefaultRouter
from .views import PurchaseRequestViewSet, PurchaseOrderViewSet

router = DefaultRouter()

router.register('purchase-requests', PurchaseRequestViewSet, basename='purchase-request')
router.register('purchase-orders', PurchaseOrderViewSet, basename='purchase-order')

urlpatterns = router.urls