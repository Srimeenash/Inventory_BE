from rest_framework import serializers
from .models import MaterialRequest, BOMItem


class BOMItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = BOMItem
        fields = "__all__"


class MaterialRequestSerializer(serializers.ModelSerializer):
    bom_items = BOMItemSerializer(many=True, read_only=True)

    class Meta:
        model = MaterialRequest
        fields = "__all__"

    def validate_status(self, value):
        allowed = ["PENDING", "APPROVED", "REJECTED"]
        if value not in allowed:
            raise serializers.ValidationError("Invalid status")
        return value

    def validate_approval_status(self, value):
        allowed = ["NOT_REQUESTED", "REQUESTED", "APPROVED", "REJECTED"]
        if value not in allowed:
            raise serializers.ValidationError("Invalid approval_status")
        return value

    def update(self, instance, validated_data):
        approval_status = validated_data.get("approval_status")

        if approval_status:
            instance.approval_status = approval_status

            # 🔥 AUTO SYNC STATUS
            if approval_status == "APPROVED":
                instance.status = "APPROVED"
            elif approval_status == "REJECTED":
                instance.status = "REJECTED"
            elif approval_status == "REQUESTED":
                instance.status = "PENDING"

        instance.save()
        return instance