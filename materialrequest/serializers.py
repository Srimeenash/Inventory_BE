from rest_framework import serializers
from .models import MaterialRequest, BOMItem, RDItem


class BOMItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = BOMItem
        fields = "__all__"


class RDItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = RDItem
        exclude = ["material_request"]


class MaterialRequestSerializer(serializers.ModelSerializer):

    bom = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True
    )

    bom_items = BOMItemSerializer(many=True, required=False)
    rd_items = RDItemSerializer(many=True, required=False)

    class Meta:
        model = MaterialRequest
        fields = "__all__"

    def create(self, validated_data):
        bom_items = validated_data.pop("bom_items", [])
        rd_items = validated_data.pop("rd_items", [])

        material_request = MaterialRequest.objects.create(**validated_data)

        for item in bom_items:
            BOMItem.objects.create(
                material_request=material_request,
                **item
            )

        for item in rd_items:
            RDItem.objects.create(
                material_request=material_request,
                **item
            )

        return material_request

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

            if approval_status == "APPROVED":
                instance.status = "APPROVED"
            elif approval_status == "REJECTED":
                instance.status = "REJECTED"
            elif approval_status == "REQUESTED":
                instance.status = "PENDING"

        instance.save()
        return instance