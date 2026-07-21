from rest_framework import serializers
from .models import BOM, BOMItem


class BOMItemSerializer(serializers.ModelSerializer):
    component_name = serializers.CharField(
        source="component.name",
        read_only=True
    )

    component_code = serializers.CharField(
        source="component.component_id",
        read_only=True
    )

    class Meta:
        model = BOMItem
        fields = [
            "id",
            "bom",
            "component",
            "component_name",
            "component_code",
            "category",
            "specifications",
            "quantity",
            "remarks",
        ]
        extra_kwargs = {
    "bom": {
        "required": False,
        "read_only": True
    }
}
        
        


class BOMSerializer(serializers.ModelSerializer):
    items = BOMItemSerializer(many=True)

    class Meta:
        model = BOM
        fields = [
            "id",
            "bom_number",
            "bom_name",
            "product_name",
            "version",
            "created_by",
            "description",
            "is_active",
            "created_at",
            "updated_at",
            "items",
        ]

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])

        bom = BOM.objects.create(**validated_data)

        for item in items_data:
            BOMItem.objects.create(
                bom=bom,
                **item
            )

        return bom