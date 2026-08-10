from rest_framework import serializers

from notifications.models import Notification

from .models import BOM, BOMItem


class BOMItemSerializer(
    serializers.ModelSerializer
):
    bom = serializers.PrimaryKeyRelatedField(
        queryset=BOM.objects.all(),
        required=False,
    )

    component_name = serializers.CharField(
        source="component.name",
        read_only=True,
    )

    component_code = serializers.CharField(
        source="component.component_id",
        read_only=True,
    )

    class Meta:
        model = BOMItem

        fields = [
            "id",
            "bom",
            "component",
            "component_name",
            "component_code",
            "category",
            "specifications",
            "quantity",
            "vendor",
            "remarks",
        ]


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

        # Every newly created BOM must wait
        # for manager approval.
        validated_data["status"] = (
            "PENDING_MANAGER"
        )

        bom = BOM.objects.create(
            **validated_data
        )

        for item_data in items_data:
            item_data.pop("bom", None)

            BOMItem.objects.create(
                bom=bom,
                **item_data,
            )

        # Remove an older duplicate notification,
        # if one exists for the same BOM.
        Notification.objects.filter(
            category="BOM",
            reference_id=str(bom.id),
            receiver="MANAGER",
        ).delete()

        # Automatically send the BOM to the
        # Manager Notifications page.
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
            setattr(instance, field, value)

        if old_status == "MANAGER_REJECTED":
            instance.status = "MODIFIED"

        instance.save()

        if items_data is not None:
            for item_data in items_data:
                item_data.pop("bom", None)

                BOMItem.objects.create(
                    bom=instance,
                    **item_data,
                )

        return instance