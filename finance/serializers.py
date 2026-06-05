from rest_framework import serializers
from .models import Invoice, Payment, FinanceLedger


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = '__all__'


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = '__all__'


class FinanceLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinanceLedger
        fields = '__all__'