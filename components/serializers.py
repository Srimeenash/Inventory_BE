from rest_framework import serializers
from .models import Component

class ComponentSerializer(serializers.ModelSerializer):
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
            'ordering_id',
            'unit_price',
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
