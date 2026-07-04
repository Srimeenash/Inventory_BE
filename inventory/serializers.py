from rest_framework import serializers
from .models import Inventory


class InventorySerializer(serializers.ModelSerializer):
    component_name = serializers.CharField(
        source="component.name",
        read_only=True
    )

    category = serializers.CharField(
        source="component.category",
        read_only=True
    )

    class Meta:
        model = Inventory
        fields = [
            "id",
            "inventory_code",

            "component",
            "component_name",
            "category",

            "vendor",

            "purchase_order",

            "quantity",
            "received_date",
            "total_price",

            "moved",
            "employee_id",
            "bom",

            "created_at",
        ]

        read_only_fields = [
            "inventory_code",
            "component_name",
            "category",
            "created_at",
        ]