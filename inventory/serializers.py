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

            "issued",

            "created_at",
        ]

        read_only_fields = [
            "component_name",
            "category",
            "created_at",
        ]

    def create(self, validated_data):
        if not validated_data.get("inventory_code"):
            validated_data["inventory_code"] = self._generate_next_inventory_code()
        return super().create(validated_data)

    @staticmethod
    def _generate_next_inventory_code():
        last = Inventory.objects.order_by("-id").first()
        if last and last.inventory_code:
            try:
                last_no = int(last.inventory_code.replace("INV", ""))
            except (ValueError, TypeError):
                last_no = 0
        else:
            last_no = 0

        return f"INV{last_no + 1:05d}"