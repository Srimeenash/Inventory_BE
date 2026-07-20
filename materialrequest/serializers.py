from rest_framework import serializers
from .models import MaterialRequest, BOMItem, RDItem


class BOMItemSerializer(serializers.ModelSerializer):
    material_request = serializers.PrimaryKeyRelatedField(read_only=True)

    component_code = serializers.CharField(
        source="component.component_id",
        read_only=True
    )

    component_name = serializers.CharField(
        source="component.name",
        read_only=True
    )

    class Meta:
        model = BOMItem
        # Hide pricing fields for BOM items in API responses
        exclude = (
    "unit_price",
    "price",
    "tax",
)

class RDItemSerializer(serializers.ModelSerializer):
    material_request = serializers.PrimaryKeyRelatedField(
        read_only=True
    )

    component_code = serializers.CharField(
        source="component.component_id",
        read_only=True
    )

    component_name = serializers.CharField(
        source="component.name",
        read_only=True
    )

    class Meta:
        model = RDItem
        exclude = (
            "unit_price",
            "price",
            "tax",
        )


class MaterialRequestSerializer(serializers.ModelSerializer):

    bom = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True
    )

    bom_items = BOMItemSerializer(many=True, required=False)
    rd_items = RDItemSerializer(many=True, required=False)

    def validate(self, attrs):
        if attrs.get("request_type") == "BOM" and not attrs.get("bom"):
            raise serializers.ValidationError({"bom": ["Please select a BOM."]})
        return attrs

    class Meta:
        model = MaterialRequest
        fields = "__all__"

    def create(self, validated_data):
        bom_items = validated_data.pop("bom_items", [])
        rd_items = validated_data.pop("rd_items", [])

        if validated_data.get("request_type") != "BOM":
            validated_data["bom"] = ""

        material_request = MaterialRequest.objects.create(**validated_data)
        print("BOM ITEMS:", bom_items)
        
        for item in bom_items:
            BOMItem.objects.create(
                material_request=material_request,
                component=item.get("component"),
                category=item.get("category", ""),
                specification=item.get("specification", ""),
                quantity=item.get("quantity", 1),
                inventory_quantity=item.get("inventory_quantity", 0),
                unit=item.get("unit", "pc"),
                unit_price=item.get("unit_price", 0),
                price=item.get("price", 0),
                tax=item.get("tax", 0),
                vendor=item.get("vendor", "N/A"),
                remarks=item.get("remarks", ""),
            )

        for item in rd_items:
            RDItem.objects.create(
                material_request=material_request,
                component=item.get("component"),
                category=item.get("category", ""),
                specifications=item.get("specifications", ""),
                quantity=item.get("quantity", 1),
                inventory_quantity=item.get("inventory_quantity", 0),
                unit_price=item.get("unit_price", 0),
                unit=item.get("unit", "pc"),
                price=item.get("price", 0),
                tax=item.get("tax", 0),
                total_price=item.get("total_price", 0),
                vendor=item.get("vendor", "N/A"),
                remarks=item.get("remarks", ""),
            )

        return material_request

    def validate_status(self, value):
        allowed = [
        "PENDING",
        "APPROVED",
        "REJECTED",
        "PO_RAISED",
    ]
        if value not in allowed:
            raise serializers.ValidationError("Invalid status")
        return value

    def validate_approval_status(self, value):
        allowed = ["NOT_REQUESTED", "REQUESTED", "MANAGER_APPROVED", "APPROVED", "REJECTED"]
        if value not in allowed:
            raise serializers.ValidationError("Invalid approval_status")
        return value

    def update(self, instance, validated_data):
        approval_status = validated_data.get("approval_status")
        rejection_reason = validated_data.get("rejection_reason")
        rejected_by = validated_data.get("rejected_by")
        po_raised = validated_data.get("po_raised")

        status = validated_data.get("status")

        if status:
            instance.status = status

        if rejection_reason is not None:
            instance.rejection_reason = rejection_reason

        if rejected_by is not None:
            instance.rejected_by = rejected_by

        if po_raised is not None:
            instance.po_raised = po_raised

            if po_raised:
                instance.status = "PO_RAISED"

        if approval_status:
            instance.approval_status = approval_status

            if approval_status == "APPROVED":
                instance.status = "APPROVED"
            elif approval_status == "REJECTED":
                instance.status = "REJECTED"
            elif approval_status == "REQUESTED" or approval_status == "MANAGER_APPROVED":
                instance.status = "PENDING"

        instance.save()
        return instance