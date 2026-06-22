# serializers.py
from rest_framework import serializers
from .models import MaterialRequest

class MaterialRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = MaterialRequest
        fields = "__all__"
