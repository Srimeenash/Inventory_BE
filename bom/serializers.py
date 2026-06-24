from rest_framework import serializers
from .models import BOM, BOMItem
from .models import BOMItem

# BOMItem serializer – exclude 'bom' so frontend doesn’t need to send it
class BOMItemSerializer(serializers.ModelSerializer):
    bom = serializers.PrimaryKeyRelatedField(
        queryset=BOM.objects.all(),
        required=False,
        write_only=True
    )

    class Meta:
        model = BOMItem
        fields = "__all__"
# BOM serializer – nests BOMItemSerializer
class BOMSerializer(serializers.ModelSerializer):
    items = BOMItemSerializer(many=True)

    class Meta:
        model = BOM
        fields = "__all__"

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])

        bom = BOM.objects.create(**validated_data)

        for item in items_data:
            BOMItem.objects.create(bom=bom, **item)

        return bom

    def update(self, instance, validated_data):
        items_data = validated_data.pop("items", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        if items_data is not None:
            instance.items.all().delete()
            for item in items_data:
                BOMItem.objects.create(bom=instance, **item)

        return instance
