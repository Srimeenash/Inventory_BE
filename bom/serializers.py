from rest_framework import serializers
from .models import BOM, BOMItem


# BOMItem serializer – exclude vendor and pricing fields for BOM pages
class BOMItemSerializer(serializers.ModelSerializer):
    component_name = serializers.CharField(
        source="component.name",
        read_only=True
    )

    class Meta:
        model = BOMItem
        fields = [
            "id",
            "component",
            "component_name",
            "component_code",
            "category",
            "specifications",
            "quantity",
            "remarks",
        ]


# BOM serializer – nests BOMItemSerializer
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
        # remove project_type if present in payload
        validated_data.pop("project_type", None)
        items_data = validated_data.pop("items", [])

        bom = BOM.objects.create(**validated_data)

        for item in items_data:
            item.pop("vendor", None)
            BOMItem.objects.create(bom=bom, **item)

        return bom

    def update(self, instance, validated_data):
        # ignore project_type from updates
        validated_data.pop("project_type", None)
        items_data = validated_data.pop("items", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        if items_data is not None:
            instance.items.all().delete()

            for item in items_data:
                item.pop("vendor", None)
                BOMItem.objects.create(bom=instance, **item)

        return instance
