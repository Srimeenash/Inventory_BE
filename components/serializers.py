from rest_framework import serializers
from django.db.models import Max
from .models import Component

class ComponentSerializer(serializers.ModelSerializer):
    
    component_id = serializers.CharField(required=False)
    class Meta:
        model = Component
        fields = [
            'id',
            'component_id',
            'name',
            'category',
            'specifications',
            'unit_of_measurements',
            'hsn_numbers',
            'sku_numbers',
            'part_numbers',
            'product_link', 
            'ordering_id',
            'unit_price',
            'tally_reference',
            'stock_quantity',
            'reorder_level',
            'total_value',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'total_value', 'created_at', 'updated_at']

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        user = self.context.get('request').user
        if user and getattr(user, 'role', None) and user.role.name in ['ENGINEER', 'ENGINEERING_MANAGER']:
            representation.pop('unit_price', None)
            representation.pop('total_value', None)
        return representation
    
    def create(self, validated_data):
    # Generate Component ID automatically if not provided
       if not validated_data.get("component_id"):
        last_id = Component.objects.aggregate(
            Max("id")
        )["id__max"] or 0

        validated_data["component_id"] = f"CMP{last_id + 1:04d}"

       return super().create(validated_data)
    
    def validate_component_id(self, value):
        qs = Component.objects.filter(component_id=value)

        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)

        if qs.exists():
            raise serializers.ValidationError(
                "Component ID already exists."
            )

        return value