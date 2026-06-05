from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    ReportLogViewSet,
    InventoryReportView,
    ProcurementReportView,
    FinanceReportView,
    BOMReportView
)

router = DefaultRouter()
router.register('report-logs', ReportLogViewSet, basename='report-logs')

urlpatterns = [
    path('', include(router.urls)),

    path('inventory-report/', InventoryReportView.as_view()),
    path('procurement-report/', ProcurementReportView.as_view()),
    path('finance-report/', FinanceReportView.as_view()),
    path('bom-report/', BOMReportView.as_view()),
]