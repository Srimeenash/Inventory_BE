from rest_framework import serializers
from decimal import Decimal

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
    component_id = serializers.IntegerField(write_only=True)

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
            "unit_price",
            "gst_percentage",
            "subtotal",
            "gst_amount",
            "total_cost",
        ]

    def validate_gst_percentage(self, value):
        if value is not None and (value < 0 or value > 100):
            raise serializers.ValidationError(
                "GST percentage must be between 0 and 100."
            )
        return value

# ---------------- PURCHASE ORDER ----------------
class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True, required=False)

    po_date = serializers.SerializerMethodField()
    expected_delivery_date = serializers.DateField(required=False, allow_null=True)

    qty = serializers.SerializerMethodField()
    unit_price = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    approval_status = serializers.ChoiceField(
        choices=[
            "NOT_REQUESTED",
            "PENDING",
            "REQUESTED",
            "MANAGER_APPROVED",
            "APPROVED",
            "REJECTED",
        ],
        required=False,
    )

    latest_approval = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = [
            "id",
            "po_number",
            "vendor_name",
            "gstin",
            "location",
            "po_date",
            "expected_delivery_date",
            "status",
            "approval_status",
            "latest_approval",
            "items",
            "qty",
            "unit_price",
            "total",
            "rejection_reason",
            "rejected_by",
        ]

    # ---------------- BASIC FIELDS ----------------
    def get_po_date(self, obj):
        return obj.created_at.date().isoformat()

    def get_qty(self, obj):
        return sum(i.quantity for i in obj.items.all())

    def get_unit_price(self, obj):
        items = obj.items.all()
        if not items:
            return 0
        return sum(i.unit_price for i in items) / len(items)

    def get_total(self, obj):
        totals = [
            item.total_cost
            for item in obj.items.all()
            if item.total_cost is not None
        ]
        return sum(totals, Decimal("0"))

    # ---------------- APPROVAL ----------------
    def get_latest_approval(self, obj):
        latest = obj.approvals.order_by("-created_at").first()

        if not latest:
            return None

        return {
            "id": latest.id,
            "action": latest.action,
            "requested_by": latest.requested_by,
            "created_at": latest.created_at,
        }

    # ---------------- CREATE ----------------
    def create(self, validated_data):
        items_data = validated_data.pop("items", [])
        approval_status = validated_data.get("approval_status", "NOT_REQUESTED")

        po = PurchaseOrder.objects.create(
            po_number=validated_data.get("po_number"),
            vendor_name=validated_data.get("vendor_name"),
            gstin=validated_data.get("gstin", ""),
            location=validated_data.get("location", ""),
            status=validated_data.get("status", "PENDING"),
            approval_status=approval_status,
            expected_delivery_date=validated_data.get("expected_delivery_date"),
        )
        Notification.objects.create(
            category="PO",
            title=f"Purchase Order {po.po_number}",
            message="New Purchase Order requires Admin approval",
            reference_id=str(po.id),
            status="PENDING_ADMIN"
        )
        for item in items_data:
            PurchaseOrderItem.objects.create(
                purchase_order=po,
                component_id=item["component_id"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
                gst_percentage=item.get("gst_percentage"),
            )

        return po

    # ---------------- UPDATE (FIXED - NO DUPLICATES) ----------------
    def update(self, instance, validated_data):
        items_data = validated_data.pop("items", [])
        deleted_items = validated_data.pop("deleted_items", [])

        # 1. DELETE removed items
        if deleted_items:
            PurchaseOrderItem.objects.filter(
                id__in=deleted_items,
                purchase_order=instance
            ).delete()

        # 2. UPDATE PO fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        instance.save()

        # 3. UPSERT ITEMS (THIS FIXES DUPLICATION)
        for item_data in items_data:
            item_id = item_data.get("id")
            component_id = item_data.get("component_id")

            # 👉 IMPORTANT FIX: prevent duplicate component rows
            existing_item = None

            if item_id:
                existing_item = PurchaseOrderItem.objects.filter(
                    id=item_id,
                    purchase_order=instance
                ).first()

            # fallback: check by component (prevents duplicate Propellers issue)
            if not existing_item:
                existing_item = PurchaseOrderItem.objects.filter(
                    purchase_order=instance,
                    component_id=component_id
                ).first()

            if existing_item:
                # UPDATE existing row
                existing_item.quantity = item_data["quantity"]
                existing_item.unit_price = item_data["unit_price"]
                existing_item.gst_percentage = item_data.get("gst_percentage")
                existing_item.save()

            else:
                # CREATE new row
                PurchaseOrderItem.objects.create(
                    purchase_order=instance,
                    component_id=component_id,
                    quantity=item_data["quantity"],
                    unit_price=item_data["unit_price"],
                    gst_percentage=item_data.get("gst_percentage"),
                )

        return instance