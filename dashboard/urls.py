from django.urls import path
from .views import DashboardView, ManualLowStockView, ManualLowStockDetailView

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("manual-low-stock/", ManualLowStockView.as_view(), name="manual-low-stock"),
    path("manual-low-stock/<int:pk>/", ManualLowStockDetailView.as_view(), name="manual-low-stock-detail"),
]