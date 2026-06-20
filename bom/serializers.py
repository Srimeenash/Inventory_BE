from rest_framework import serializers
from .models import BOM, BOMItem
from components.models import Component



class BOMItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = BOMItem
        fields = "__all__"


class BOMSerializer(serializers.ModelSerializer):
    items = BOMItemSerializer(many=True)

    class Meta:
        model = BOM
        fields = "__all__"

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])

        bom = BOM.objects.create(**validated_data)

        for item in items_data:
            BOMItem.objects.create(
                bom=bom,
                **item
            )

        return bom

class BOMSerializer(serializers.ModelSerializer):
    items = BOMItemSerializer(many=True, required=False)

    class Meta:
        model = BOM
        fields = [
            'id',
            'bom_number',
            'product_name',
            'version',
            'project_type',
            'bom_name',
            'created_by',
            'description',
            'is_active',
            'created_at',
            'updated_at',
            'items',
        ]

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])

        bom = BOM.objects.create(**validated_data)

        for item_data in items_data:
            BOMItem.objects.create(
                bom=bom,
                component=item_data['component'],
                category=item_data.get('category'),
                specifications=item_data.get('specifications'),
                quantity=item_data.get('quantity', 1),
                price=item_data.get('price', 0),
                tax=item_data.get('tax', 0),
                vendor=item_data.get('vendor'),
            )

        return bom