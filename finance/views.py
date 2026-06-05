from rest_framework import viewsets
from .models import Invoice, Payment, FinanceLedger
from .serializers import InvoiceSerializer, PaymentSerializer, FinanceLedgerSerializer
from accounts.permissions import IsManager


class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = Invoice.objects.all().order_by('-created_at')
    serializer_class = InvoiceSerializer
    permission_classes = [IsManager]


class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.all().order_by('-paid_date')
    serializer_class = PaymentSerializer
    permission_classes = [IsManager]


class FinanceLedgerViewSet(viewsets.ModelViewSet):
    queryset = FinanceLedger.objects.all().order_by('-created_at')
    serializer_class = FinanceLedgerSerializer
    permission_classes = [IsManager]