from rest_framework import serializers
from .models import StockIn, StockInItem, StockOut, StockOutItem, InventoryLedger


class StockInItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockInItem
        fields = '__all__'


class StockInSerializer(serializers.ModelSerializer):
    items = StockInItemSerializer(many=True, read_only=True)

    class Meta:
        model = StockIn
        fields = '__all__'


class StockOutItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockOutItem
        fields = '__all__'


class StockOutSerializer(serializers.ModelSerializer):
    items = StockOutItemSerializer(many=True, read_only=True)

    class Meta:
        model = StockOut
        fields = '__all__'


class InventoryLedgerSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventoryLedger
        fields = '__all__'