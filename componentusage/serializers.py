from rest_framework import serializers
from .models import ComponentUsage

class ComponentUsageSerializer(serializers.ModelSerializer):

    issued_date = serializers.DateField(required=False, allow_null=True)
    received_date = serializers.DateField(required=False, allow_null=True)

    class Meta:
        model = ComponentUsage
        fields = "__all__"

    def validate(self, data):
        if data.get("received_date") and not data.get("issued_date"):
            raise serializers.ValidationError("Cannot receive before issuing")
        return data