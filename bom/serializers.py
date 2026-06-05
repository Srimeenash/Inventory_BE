from rest_framework import serializers
from .models import BOM, BOMItem


class BOMItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = BOMItem
        fields = '__all__'


class BOMSerializer(serializers.ModelSerializer):
    items = BOMItemSerializer(many=True, read_only=True)

    class Meta:
        model = BOM
        fields = '__all__'