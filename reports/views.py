from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import viewsets

from .models import ReportLog
from .serializers import ReportLogSerializer

from components.models import Component
from procurement.models import PurchaseRequest, PurchaseOrder
from finance.models import Invoice
from bom.models import BOM




class ReportLogViewSet(viewsets.ModelViewSet):
    queryset = ReportLog.objects.all().order_by('-created_at')
    serializer_class = ReportLogSerializer


# 📊 INVENTORY REPORT
class InventoryReportView(APIView):

    def get(self, request):
        components = Component.objects.all()

        data = {
            "total_components": components.count(),
            "total_stock_value": sum(c.unit_price * c.stock_quantity for c in components),
            "low_stock_items": components.filter(stock_quantity__lte=5).count(),
            "active_components": components.filter(is_active=True).count(),
        }

        return Response(data)


# 📦 PROCUREMENT REPORT
class ProcurementReportView(APIView):

    def get(self, request):
        data = {
            "total_pr": PurchaseRequest.objects.count(),
            "pending_pr": PurchaseRequest.objects.filter(status='PENDING').count(),
            "approved_pr": PurchaseRequest.objects.filter(status='APPROVED').count(),
            "total_po": PurchaseOrder.objects.count(),
        }

        return Response(data)


# 💰 FINANCE REPORT
class FinanceReportView(APIView):

    def get(self, request):
        data = {
            "total_invoices": Invoice.objects.count(),
            "pending_invoices": Invoice.objects.filter(status='PENDING').count(),
            "paid_invoices": Invoice.objects.filter(status='PAID').count(),
        }

        return Response(data)


# 🧱 BOM REPORT
class BOMReportView(APIView):

    def get(self, request):
        data = {
            "total_bom": BOM.objects.count(),
            "active_bom": BOM.objects.filter(is_active=True).count(),
        }

        return Response(data)