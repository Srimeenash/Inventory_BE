from rest_framework import serializers

from notifications.models import Notification

from decimal import Decimal

from .models import BOM, BOMItem, MasterBOMPricing, ProjectBOM


class BOMItemSerializer(serializers.ModelSerializer):
    bom = serializers.PrimaryKeyRelatedField(
        queryset=BOM.objects.all(),
        required=False,
    )

    component_code = serializers.CharField(
        source="component.component_id",
        read_only=True,
        default="",
    )

    component_type = serializers.CharField(
        source="component.component_type",
        read_only=True,
        default="",
    )

    class Meta:
        model = BOMItem
        fields = [
            "id",
            "bom",
            "component",
            "component_code",
            "component_type",
            "category",
            "specifications",
            "quantity",
            "unit",
            "vendor",
            "remarks",
        ]

    def validate_quantity(self, value):
        if int(value or 0) <= 0:
            raise serializers.ValidationError(
                "Quantity must be greater than 0."
            )
        return value

    def validate_unit(self, value):
        return str(value or "").strip()


class BOMSerializer(serializers.ModelSerializer):
    items = BOMItemSerializer(
        many=True,
        required=False,
    )

    class Meta:
        model = BOM
        fields = [
            "id",
            "bom_number",
            "bom_name",
            "product_name",
            "version",
            "created_by",
            "description",
            "status",
            "manager_rejection_reason",
            "manager_rejected_by",
            "manager_rejected_at",
            "manager_approved_by",
            "manager_approved_at",
            "is_active",
            "created_at",
            "updated_at",
            "items",
        ]
        read_only_fields = [
            "manager_rejected_at",
            "manager_approved_at",
            "created_at",
            "updated_at",
        ]

    def create(self, validated_data):
        items_data = validated_data.pop(
            "items",
            [],
        )
        validated_data["status"] = (
            "PENDING_MANAGER"
        )

        bom = BOM.objects.create(
            **validated_data
        )

        for item_data in items_data:
            item_data.pop("bom", None)
            item_data["unit"] = str(
                item_data.get("unit") or ""
            ).strip()

            BOMItem.objects.create(
                bom=bom,
                **item_data,
            )

        Notification.objects.filter(
            category="BOM",
            reference_id=str(bom.id),
            receiver="MANAGER",
        ).delete()

        Notification.objects.create(
            category="BOM",
            title=(
                f"BOM Approval Request - "
                f"{bom.bom_number}"
            ),
            message=(
                f"BOM {bom.bom_number} was created "
                f"by {bom.created_by} and requires "
                f"manager approval."
            ),
            reference_id=str(bom.id),
            status="PENDING_MANAGER",
            receiver="MANAGER",
            is_read=False,
        )

        return bom

    def update(
        self,
        instance,
        validated_data,
    ):
        items_data = validated_data.pop(
            "items",
            None,
        )

        old_status = instance.status

        for field, value in (
            validated_data.items()
        ):
            setattr(
                instance,
                field,
                value,
            )

        if old_status == "MANAGER_REJECTED":
            instance.status = "MODIFIED"

        instance.save()

        if items_data is not None:
            for item_data in items_data:
                item_data.pop(
                    "bom",
                    None,
                )
                item_data["unit"] = str(
                    item_data.get("unit")
                    or ""
                ).strip()

                BOMItem.objects.create(
                    bom=instance,
                    **item_data,
                )

        return instance


class ProjectBOMSerializer(serializers.ModelSerializer):
    project_bom_number = serializers.CharField(read_only=True)
    source_bom_id = serializers.IntegerField(read_only=True)
    status = serializers.SerializerMethodField()
    approval_status = serializers.SerializerMethodField()
    component_count = serializers.SerializerMethodField()

    class Meta:
        model = ProjectBOM
        fields = (
            "id", "project_bom_number", "material_request_number",
            "source_bom_id", "source_bom_number", "bom_name", "product_name",
            "version", "project", "created_by", "created_at", "status",
            "approval_status", "component_count", "items_snapshot",
        )
        read_only_fields = fields

    def get_component_count(self, obj):
        return sum(
            item.get("change_type") != "DELETED"
            for item in obj.items_snapshot
        )

    def get_status(self, obj):
        return getattr(obj.material_request, "status", None) or obj.status_snapshot

    def get_approval_status(self, obj):
        return getattr(obj.material_request, "approval_status", None) or obj.approval_status_snapshot


class MasterBOMPricingSerializer(serializers.ModelSerializer):
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0"),
        required=False, allow_null=True,
    )
    unit_price = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"),
        required=False, allow_null=True,
    )
    discount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"),
        required=False, allow_null=True,
    )
    gst_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"),
        max_value=Decimal("100"), required=False, allow_null=True,
    )
    freight_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"),
        required=False, allow_null=True,
    )
    freight_gst_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"),
        max_value=Decimal("100"), required=False, allow_null=True,
    )

    class Meta:
        model = MasterBOMPricing
        fields = (
            "quantity", "uom", "vendor", "unit_price", "discount",
            "gst_percent", "freight_cost", "freight_gst_percent",
        )

    def validate(self, attrs):
        quantity = attrs.get("quantity", getattr(self.instance, "quantity", None))
        unit_price = attrs.get("unit_price", getattr(self.instance, "unit_price", None))
        discount = attrs.get("discount", getattr(self.instance, "discount", None))
        if quantity is not None and unit_price is not None and discount is not None:
            if discount > quantity * unit_price:
                raise serializers.ValidationError({
                    "discount": "Discount cannot exceed quantity × unit price."
                })
        return attrs
