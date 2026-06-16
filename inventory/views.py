from rest_framework import viewsets
from .models import StockIn, StockOut, InventoryLedger
from .serializers import StockInSerializer, StockOutSerializer, InventoryLedgerSerializer



class StockInViewSet(viewsets.ModelViewSet):
    queryset = StockIn.objects.all().order_by('-received_date')
    serializer_class = StockInSerializer


class StockOutViewSet(viewsets.ModelViewSet):
    queryset = StockOut.objects.all().order_by('-issued_date')
    serializer_class = StockOutSerializer



class InventoryLedgerViewSet(viewsets.ModelViewSet):
    queryset = InventoryLedger.objects.all().order_by('-created_at')
    serializer_class = InventoryLedgerSerializer
