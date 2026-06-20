from rest_framework import serializers
from .models import ComponentUsage

class ComponentUsageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ComponentUsage
        fields = "__all__"
