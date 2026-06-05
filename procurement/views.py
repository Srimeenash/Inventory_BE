from rest_framework import viewsets
from .models import PurchaseRequest, PurchaseOrder
from .serializers import PurchaseRequestSerializer, PurchaseOrderSerializer
from accounts.permissions import IsManager


class PurchaseRequestViewSet(viewsets.ModelViewSet):
    queryset = PurchaseRequest.objects.all().order_by('-created_at')
    serializer_class = PurchaseRequestSerializer
    permission_classes = [IsManager]


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    queryset = PurchaseOrder.objects.all().order_by('-created_at')
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsManager]