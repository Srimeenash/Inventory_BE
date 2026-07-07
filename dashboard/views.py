from rest_framework.views import APIView
from rest_framework.response import Response
from components.models import Component
from procurement.models import PurchaseRequest, PurchaseOrder
from approvals.models import ApprovalRequest
from bom.models import BOM


from rest_framework import status
from rest_framework.views import APIView
from .models import ManualLowStock
from .serializers import ManualLowStockSerializer

class DashboardView(APIView):

    def get(self, request):

        # COMPONENTS
        components = Component.objects.all()
        total_components = components.count()

        total_stock_value = sum(
            c.unit_price * c.stock_quantity for c in components
        )

        low_stock_items = components.filter(
            stock_quantity__lte=5
        ).count()

        # PROCUREMENT
        pending_pr = PurchaseRequest.objects.filter(status='PENDING').count()
        approved_pr = PurchaseRequest.objects.filter(status='APPROVED').count()

        pending_po = PurchaseOrder.objects.filter(status='DRAFT').count()

        # APPROVALS
        pending_approvals = ApprovalRequest.objects.filter(status='PENDING').count()

        # BOM
        total_bom = BOM.objects.count()
        active_bom = BOM.objects.filter(is_active=True).count()

        data = {
            "total_components": total_components,
            "total_stock_value": total_stock_value,
            "low_stock_items": low_stock_items,
            "pending_pr": pending_pr,
            "approved_pr": approved_pr,
            "pending_po": pending_po,
            "pending_approvals": pending_approvals,
            "total_bom": total_bom,
            "active_bom": active_bom,
        }

        serializer = DashboardSerializer(data)
        return Response(serializer.data)

from rest_framework import status
from rest_framework.views import APIView
from .models import ManualLowStock
from .serializers import ManualLowStockSerializer


class ManualLowStockView(APIView):

    def get(self, request):
        data = ManualLowStock.objects.all().order_by("-created_at")
        serializer = ManualLowStockSerializer(data, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = ManualLowStockSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)