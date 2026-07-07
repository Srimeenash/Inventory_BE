from django.urls import path
from .views import DashboardView, ManualLowStockView

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("manual-low-stock/", ManualLowStockView.as_view(), name="manual-low-stock"),
]