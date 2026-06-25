# from rest_framework import serializers
# from components.models import Component
# from .models import PurchaseOrder, PurchaseOrderItem, PurchaseRequest, PurchaseRequestItem


# # ---------------- COMPONENT ----------------
# class ComponentMiniSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = Component
#         fields = ["id", "component_id", "name"]


# # ---------------- PURCHASE REQUEST ----------------
# class PurchaseRequestItemSerializer(serializers.ModelSerializer):
#     component = ComponentMiniSerializer(read_only=True)

#     class Meta:
#         model = PurchaseRequestItem
#         fields = ["id", "component", "quantity", "remarks"]


# class PurchaseRequestSerializer(serializers.ModelSerializer):
#     items = PurchaseRequestItemSerializer(many=True, read_only=True)

#     class Meta:
#         model = PurchaseRequest
#         fields = "__all__"


# # ---------------- PURCHASE ORDER ITEM ----------------
# class PurchaseOrderItemSerializer(serializers.ModelSerializer):
#     component = ComponentMiniSerializer(read_only=True)
#     component_id = serializers.IntegerField(write_only=True)

#     subtotal = serializers.ReadOnlyField()
#     gst_amount = serializers.ReadOnlyField()
#     total_cost = serializers.ReadOnlyField()

#     class Meta:
#         model = PurchaseOrderItem
#         fields = [
#             "id",
#             "component",
#             "component_id",
#             "quantity",
#             "unit_price",
#             "gst_percentage",
#             "subtotal",
#             "gst_amount",
#             "total_cost",
#         ]


# # ---------------- PURCHASE ORDER ----------------
# class PurchaseOrderSerializer(serializers.ModelSerializer):
#     items = PurchaseOrderItemSerializer(many=True, required=False)

#     po_date = serializers.SerializerMethodField()
#     expected_delivery_date = serializers.DateField(required=False, allow_null=True)

#     qty = serializers.SerializerMethodField()
#     unit_price = serializers.SerializerMethodField()
#     total = serializers.SerializerMethodField()

#     # 🔥 APPROVAL FIELDS
#     approval_status = serializers.SerializerMethodField()
#     latest_approval = serializers.SerializerMethodField()

#     class Meta:
#         model = PurchaseOrder
#         fields = [
#             "id",
#             "po_number",
#             "vendor_name",
#             "gstin",
#             "location",
#             "po_date",
#             "expected_delivery_date",
#             "status",
#             "approval_status",
#             "latest_approval",
#             "items",
#             "qty",
#             "unit_price",
#             "total",
#         ]

#     # ---------------- BASIC FIELDS ----------------
#     def get_po_date(self, obj):
#         return obj.created_at.date().isoformat()

#     def get_qty(self, obj):
#         return sum(i.quantity for i in obj.items.all())

#     def get_unit_price(self, obj):
#         items = obj.items.all()
#         if not items:
#             return 0
#         return sum(i.unit_price for i in items) / len(items)

#     def get_total(self, obj):
#         return sum(item.total_cost for item in obj.items.all())

#     # ---------------- APPROVAL LOGIC ----------------
#     def get_approval_status(self, obj):
#         latest = obj.approvals.order_by("-created_at").first()
#         if not latest:
#             return "NOT_REQUESTED"
#         return latest.action

#     def get_latest_approval(self, obj):
#         latest = obj.approvals.order_by("-created_at").first()

#         if not latest:
#             return None

#         return {
#             "id": latest.id,
#             "action": latest.action,
#             "requested_by": latest.requested_by,
#             "created_at": latest.created_at,
#         }

#     # ---------------- CREATE ----------------
#     def create(self, validated_data):
#         items_data = self.initial_data.get("items", [])

#         po = PurchaseOrder.objects.create(
#             po_number=validated_data.get("po_number"),
#             vendor_name=validated_data.get("vendor_name"),
#             gstin=validated_data.get("gstin", ""),
#             location=validated_data.get("location", ""),
#             status=validated_data.get("status", "PENDING"),
#             expected_delivery_date=validated_data.get("expected_delivery_date"),
#         )

#         for item in items_data:
#             PurchaseOrderItem.objects.create(
#                 purchase_order=po,
#                 component_id=item["component_id"],
#                 quantity=item["quantity"],
#                 unit_price=item["unit_price"],
#             )

#         return po

from rest_framework import serializers
from decimal import Decimal
from components.models import Component
from .models import PurchaseOrder, PurchaseOrderItem, PurchaseRequest, PurchaseRequestItem
from decimal import Decimal

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

    gst_percentage = serializers.FloatField(required=False, allow_null=True)

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

    latest_approval = serializers.SerializerMethodField()
    approval_status = serializers.ChoiceField(
        choices=["NOT_REQUESTED", "REQUESTED", "APPROVED", "REJECTED"],
        required=False
    )
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

        approval_status = validated_data.get("approval_status", "NOT_REQUESTED")

        po = PurchaseOrder.objects.create(
            po_number=validated_data.get("po_number"),
            vendor_name=validated_data.get("vendor_name"),
            gstin=validated_data.get("gstin", ""),
            location=validated_data.get("location", ""),
            status=validated_data.get("status", "PENDING"),
            approval_status=approval_status,   # ✅ THIS WAS MISSING
            expected_delivery_date=validated_data.get("expected_delivery_date"),
        )

        for item in items_data:
            PurchaseOrderItem.objects.create(
                purchase_order=po,
                component_id=item["component_id"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
                gst_percentage=item.get("gst_percentage", None),
            )

        return po


    def update(self, instance, validated_data):
        items_data = self.initial_data.get("items", [])

        new_status = validated_data.get("status")

        if new_status:
            instance.status = new_status
            instance.approval_status = new_status  # 🔥 sync both fields

        instance.save()

        for item_data in items_data:
            item_id = item_data.get("id")

            try:
                po_item = instance.items.get(id=item_id)

                po_item.unit_price = item_data.get("unit_price", po_item.unit_price)
                po_item.gst_percentage = item_data.get("gst_percentage", po_item.gst_percentage)
                po_item.save()

            except PurchaseOrderItem.DoesNotExist:
                pass

        return instance