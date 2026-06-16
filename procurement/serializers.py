from rest_framework import serializers
from .models import PurchaseOrder, PurchaseOrderItem
from .models import PurchaseRequest, PurchaseRequestItem


class PurchaseRequestItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseRequestItem
        fields = "__all__"


class PurchaseRequestSerializer(serializers.ModelSerializer):
    items = PurchaseRequestItemSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseRequest
        fields = "__all__"

class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseOrderItem
        fields = "__all__"


class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True, required=False)

    class Meta:
        model = PurchaseOrder
        fields = "__all__"

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])

        # 1. create Purchase Order (header)
        po = PurchaseOrder.objects.create(**validated_data)

        # 2. create Purchase Order Items (child rows)
        for item in items_data:
            PurchaseOrderItem.objects.create(
                purchase_order=po,
                **item
            )

        return po