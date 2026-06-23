from rest_framework import serializers
from components.models import Component
from .models import PurchaseOrder, PurchaseOrderItem, PurchaseRequest, PurchaseRequestItem


# ---------------- COMPONENT ----------------
class ComponentMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Component
        fields = ["id", "component_id", "name"]


# ---------------- PURCHASE REQUEST ----------------
class PurchaseRequestItemSerializer(serializers.ModelSerializer):
    component = ComponentMiniSerializer(read_only=True)

    class Meta:
        model = PurchaseRequestItem
        fields = ["id", "component", "quantity", "remarks"]


class PurchaseRequestSerializer(serializers.ModelSerializer):
    items = PurchaseRequestItemSerializer(many=True, read_only=True)

    class Meta:
        model = PurchaseRequest
        fields = "__all__"


# ---------------- PURCHASE ORDER ITEM ----------------
class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    component = ComponentMiniSerializer(read_only=True)
    component_id = serializers.IntegerField(write_only=True)

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


# ---------------- PURCHASE ORDER ----------------
class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True, required=False)

    po_date = serializers.SerializerMethodField()
    expected_delivery_date = serializers.DateField(required=False, allow_null=True)

    qty = serializers.SerializerMethodField()
    unit_price = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    # 🔥 APPROVAL FIELDS
    approval_status = serializers.SerializerMethodField()
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
        ]

    # ---------------- BASIC FIELDS ----------------
    def get_po_date(self, obj):
        return obj.created_at.date().isoformat()

    def get_qty(self, obj):
        return sum(i.quantity for i in obj.items.all())

    def get_unit_price(self, obj):
        items = obj.items.all()
        return sum(i.unit_price for i in items) / len(items) if items else 0

    def get_total(self, obj):
        return sum(i.total_cost for i in obj.items.all())

    # ---------------- APPROVAL LOGIC ----------------
    def get_approval_status(self, obj):
        latest = obj.approvals.order_by("-created_at").first()
        if not latest:
            return "NOT_REQUESTED"
        return latest.action

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
        items_data = self.initial_data.get("items", [])

        po = PurchaseOrder.objects.create(
            po_number=validated_data.get("po_number"),
            vendor_name=validated_data.get("vendor_name"),
            gstin=validated_data.get("gstin", ""),
            location=validated_data.get("location", ""),
            status=validated_data.get("status", "PENDING"),
            expected_delivery_date=validated_data.get("expected_delivery_date"),
        )

        for item in items_data:
            PurchaseOrderItem.objects.create(
                purchase_order=po,
                component_id=item["component_id"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
            )

        return po