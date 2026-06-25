from rest_framework.routers import DefaultRouter
from .views import StockInViewSet, StockOutViewSet, InventoryLedgerViewSet

router = DefaultRouter()

router.register('stock-in', StockInViewSet, basename='stock-in')
router.register('stock-out', StockOutViewSet, basename='stock-out')
router.register('ledger', InventoryLedgerViewSet, basename='ledger')

urlpatterns = router.urls

from rest_framework.routers import DefaultRouter
from django.urls import path, include
from .views import StockInViewSet, StockOutViewSet, InventoryLedgerViewSet, InventoryBreakdownView

router = DefaultRouter()
router.register('stock-in', StockInViewSet, basename='stock-in')
router.register('stock-out', StockOutViewSet, basename='stock-out')
router.register('ledger', InventoryLedgerViewSet, basename='ledger')

urlpatterns = [
    path('', include(router.urls)),
    path('breakdown/', InventoryBreakdownView.as_view(), name='inventory-breakdown'),
]
