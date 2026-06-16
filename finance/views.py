from rest_framework import viewsets
from .models import Invoice, Payment, FinanceLedger
from .serializers import InvoiceSerializer, PaymentSerializer, FinanceLedgerSerializer



class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = Invoice.objects.all().order_by('-created_at')
    serializer_class = InvoiceSerializer



class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.all().order_by('-paid_date')
    serializer_class = PaymentSerializer



class FinanceLedgerViewSet(viewsets.ModelViewSet):
    queryset = FinanceLedger.objects.all().order_by('-created_at')
    serializer_class = FinanceLedgerSerializer
