from rest_framework.routers import DefaultRouter
from .views import InvoiceViewSet, PaymentViewSet, FinanceLedgerViewSet

router = DefaultRouter()

router.register('invoices', InvoiceViewSet, basename='invoice')
router.register('payments', PaymentViewSet, basename='payment')
router.register('ledger', FinanceLedgerViewSet, basename='ledger')

urlpatterns = router.urls