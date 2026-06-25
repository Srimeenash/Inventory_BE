from rest_framework import viewsets
from .models import StockIn, StockOut, InventoryLedger
from .serializers import StockInSerializer, StockOutSerializer, InventoryLedgerSerializer
from rest_framework.views import APIView
from rest_framework.response import Response
from django.db.models import Sum
from .models import InventoryLedger


class StockInViewSet(viewsets.ModelViewSet):
    queryset = StockIn.objects.all().order_by('-received_date')
    serializer_class = StockInSerializer


class StockOutViewSet(viewsets.ModelViewSet):
    queryset = StockOut.objects.all().order_by('-issued_date')
    serializer_class = StockOutSerializer



class InventoryLedgerViewSet(viewsets.ModelViewSet):
    queryset = InventoryLedger.objects.all().order_by('-created_at')
    serializer_class = InventoryLedgerSerializer

class InventoryBreakdownView(APIView):
    def get(self, request):
        # Aggregate stock quantities by category
        data = (
            InventoryLedger.objects
            .values("category")
            .annotate(value=Sum("quantity"))
            .order_by("category")
        )
        formatted = [{"name": d["category"], "value": d["value"]} for d in data]
        return Response(formatted)