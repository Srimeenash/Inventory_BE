from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from components.models import Component
from .models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseRequest,
    PurchaseRequestItem,
)
from components.models import Component
from .models import PurchaseOrder, PurchaseOrderItem, PurchaseRequest, PurchaseRequestItem
from notifications.models import Notification

# ---------------- COMPONENT ----------------
class ComponentMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Component
        fields = ["id", "component_id", "name"]


# ---------------- PURCHASE REQUEST ITEM ----------------
class PurchaseRequestItemSerializer(serializers.ModelSerializer):
    component = ComponentMiniSerializer(read_only=True)

    class Meta:
        model = PurchaseRequestItem
        fields = ["id", "component", "quantity", "remarks"]


# ---------------- PURCHASE REQUEST ----------------
class PurchaseRequestSerializer(serializers.ModelSerializer):
    items = PurchaseRequestItemSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseRequest
        fields = "__all__"


# ---------------- PURCHASE ORDER ITEM ----------------
class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    component = ComponentMiniSerializer(read_only=True)

    component_id = serializers.PrimaryKeyRelatedField(
        source="component",
        queryset=Component.objects.all(),
        write_only=True,
    )

    received_quantity = serializers.IntegerField(
        read_only=True
    )

    remaining_quantity = serializers.IntegerField(
        read_only=True
    )

    gst_percentage = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=0,
        max_value=100,
    )

    subtotal = serializers.ReadOnlyField()
    gst_amount = serializers.ReadOnlyField()
    total_cost = serializers.ReadOnlyField()

    class Meta:
        model = PurchaseOrderItem

        fields = [
            "id",
            "component",
            "component_id",
            "quantity",
            "received_quantity",
            "remaining_quantity",
            "unit_price",
            "gst_percentage",
            "subtotal",
            "gst_amount",
            "total_cost",
        ]

        read_only_fields = [
            "id",
            "received_quantity",
            "remaining_quantity",
            "subtotal",
            "gst_amount",
            "total_cost",
        ]

    def validate_gst_percentage(self, value):
        if value is not None and not 0 <= value <= 100:
            raise serializers.ValidationError(
                "GST percentage must be between 0 and 100."
            )

        return value


# ---------------- PURCHASE ORDER ----------------
class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(
        many=True,
        required=False,
    )

    deleted_items = serializers.ListField(
        child=serializers.IntegerField(),
        write_only=True,
        required=False,
    )

    po_date = serializers.SerializerMethodField()
    qty = serializers.SerializerMethodField()
    unit_price = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    total_received_quantity = (
        serializers.SerializerMethodField()
    )

    total_remaining_quantity = (
        serializers.SerializerMethodField()
    )

    latest_approval = (
        serializers.SerializerMethodField()
    )

    class Meta:
        model = PurchaseOrder

        fields = [
            "id",
            "po_number",
            "vendor_name",
            "gstin",
            "location",
            "ordered_date",
            "po_date",
            "expected_delivery_date",
            "remarks",
            "finance_remarks",
            "status",
            "approval_status",
            "latest_approval",
            "source_mr_number",
            "items",
            "deleted_items",
            "qty",
            "unit_price",
            "total",
            "total_received_quantity",
            "total_remaining_quantity",
            "rejection_reason",
            "rejected_by",
            "approved_by",
            "approved_at",
            "created_at",
        ]

        read_only_fields = [
            "id",
            "po_date",
            "qty",
            "unit_price",
            "total",
            "total_received_quantity",
            "total_remaining_quantity",
            "latest_approval",
            "created_at",
        ]

    # --------------------------------------------------
    # Calculated fields
    # --------------------------------------------------
    def get_po_date(self, obj):
        if obj.ordered_date:
            return obj.ordered_date.isoformat()

        if obj.created_at:
            return obj.created_at.date().isoformat()

        return None

    def get_qty(self, obj):
        return sum(
            item.quantity
            for item in obj.items.all()
        )

    def get_unit_price(self, obj):
        items = list(obj.items.all())

        if not items:
            return Decimal("0")

        total_price = sum(
            (
                item.unit_price or Decimal("0")
                for item in items
            ),
            Decimal("0"),
        )

        return total_price / len(items)

    def get_total(self, obj):
        return sum(
            (
                item.total_cost or Decimal("0")
                for item in obj.items.all()
            ),
            Decimal("0"),
        )

    def get_total_received_quantity(self, obj):
        return sum(
            item.received_quantity
            for item in obj.items.all()
        )

    def get_total_remaining_quantity(self, obj):
        return sum(
            item.remaining_quantity
            for item in obj.items.all()
        )

    def get_latest_approval(self, obj):
        latest = (
            obj.approvals
            .order_by("-created_at")
            .first()
        )

        if not latest:
            return None

        return {
            "id": latest.id,
            "action": latest.action,
            "requested_by": latest.requested_by,
            "approved_by": latest.approved_by,
            "finance_remarks": latest.finance_remarks,
            "created_at": latest.created_at,
        }

    # --------------------------------------------------
    # Create Purchase Order
    # --------------------------------------------------
    @transaction.atomic
    def create(self, validated_data):
        items_data = validated_data.pop(
            "items",
            [],
        )

        validated_data.pop(
            "deleted_items",
            None,
        )

        if not validated_data.get(
            "approval_status"
        ):
            validated_data[
                "approval_status"
            ] = "NOT_REQUESTED"

        purchase_order = (
            PurchaseOrder.objects.create(
                **validated_data
            )
        )

        for item_data in items_data:
            PurchaseOrderItem.objects.create(
                purchase_order=purchase_order,
                **item_data,
            )

        return purchase_order

    # --------------------------------------------------
    # Update Purchase Order
    # --------------------------------------------------
    @transaction.atomic
    def update(
        self,
        instance,
        validated_data,
    ):
        items_data = validated_data.pop(
            "items",
            None,
        )

        deleted_items = validated_data.pop(
            "deleted_items",
            [],
        )

        if deleted_items:
            PurchaseOrderItem.objects.filter(
                id__in=deleted_items,
                purchase_order=instance,
            ).delete()

        for attribute, value in (
            validated_data.items()
        ):
            setattr(
                instance,
                attribute,
                value,
            )

        instance.save()

        # Do not modify PO items during a normal
        # status-only PATCH request.
        if items_data is None:
            return instance

        for item_data in items_data:
            item_id = item_data.pop(
                "id",
                None,
            )

            component = item_data.get(
                "component"
            )

            existing_item = None

            if item_id:
                existing_item = (
                    PurchaseOrderItem.objects
                    .filter(
                        id=item_id,
                        purchase_order=instance,
                    )
                    .first()
                )

            if (
                not existing_item and
                component
            ):
                existing_item = (
                    PurchaseOrderItem.objects
                    .filter(
                        purchase_order=instance,
                        component=component,
                    )
                    .first()
                )

            if existing_item:
                existing_item.quantity = (
                    item_data.get(
                        "quantity",
                        existing_item.quantity,
                    )
                )

                existing_item.unit_price = (
                    item_data.get(
                        "unit_price",
                        existing_item.unit_price,
                    )
                )

                existing_item.gst_percentage = (
                    item_data.get(
                        "gst_percentage",
                        existing_item.gst_percentage,
                    )
                )

                existing_item.save(
                    update_fields=[
                        "quantity",
                        "unit_price",
                        "gst_percentage",
                    ]
                )
            else:
                PurchaseOrderItem.objects.create(
                    purchase_order=instance,
                    **item_data,
                )

        return instance