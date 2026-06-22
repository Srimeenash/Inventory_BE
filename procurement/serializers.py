# from components.models import Component
# from rest_framework import serializers
# from .models import PurchaseOrder, PurchaseOrderItem, PurchaseRequest, PurchaseRequestItem


# class ComponentMiniSerializer(serializers.ModelSerializer):
#     class Meta:
#         model = Component
#         fields = ["id", "component_id", "name"]

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


# class PurchaseOrderSerializer(serializers.ModelSerializer):

#     items = PurchaseOrderItemSerializer(many=True, required=False)

#     qty = serializers.SerializerMethodField()
#     unit_price = serializers.SerializerMethodField()
#     total = serializers.SerializerMethodField()

#     po_date = serializers.SerializerMethodField()

#     expected_delivery = serializers.DateField(
#         source="expected_delivery_date",
#         required=False,
#         allow_null=True
#     )

#     class Meta:
#         model = PurchaseOrder
#         fields = [
#             "id",
#             "po_number",
#             "vendor_name",
#             "gstin",
#             "location",
#             "po_date",
#             "expected_delivery",
#             "status",
#             "items",
#             "qty",
#             "unit_price",
#             "total",
#         ]

#     # ---------- OUTPUT HELPERS ----------
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
#         return sum(i.total_cost for i in obj.items.all())

#     # ---------- CREATE ----------
#     def create(self, validated_data):
#         items_data = self.initial_data.get("items", [])

#         purchase_order = PurchaseOrder.objects.create(
#             po_number=validated_data.get("po_number"),
#             vendor_name=validated_data.get("vendor_name"),
#             gstin=validated_data.get("gstin", ""),
#             location=validated_data.get("location", ""),
#             status=validated_data.get("status", "PENDING"),
#             expected_delivery_date=validated_data.get("expected_delivery_date"),
#         )

#         for item in items_data:
#             PurchaseOrderItem.objects.create(
#                 purchase_order=purchase_order,
#                 component_id=item["component_id"],
#                 quantity=item["quantity"],
#                 unit_price=item["unit_price"],
#             )

#         return purchase_order


# class PurchaseRequestItemSerializer(serializers.ModelSerializer):
#     component_name = serializers.CharField(source="component.name", read_only=True)

#     class Meta:
#         model = PurchaseRequestItem
#         fields = ["id", "component", "component_name", "quantity", "remarks"]


# class PurchaseRequestSerializer(serializers.ModelSerializer):
#     items = PurchaseRequestItemSerializer(many=True, read_only=True)

#     class Meta:
#         model = PurchaseRequest
#         fields = [
#             "id",
#             "pr_number",
#             "requested_by",
#             "department",
#             "remarks",
#             "status",
#             "created_at",
#             "items",
#         ]

from rest_framework import serializers
from components.models import Component
from .models import PurchaseOrder, PurchaseOrderItem, PurchaseRequest, PurchaseRequestItem

class ComponentMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Component
        fields = ["id", "component_id", "name"]


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


class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True, required=False)

    po_date = serializers.SerializerMethodField()
    expected_delivery_date = serializers.DateField(required=False, allow_null=True)

    qty = serializers.SerializerMethodField()
    unit_price = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

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
            "items",
            "qty",
            "unit_price",
            "total",
        ]

    def get_po_date(self, obj):
        return obj.created_at.date().isoformat()

    def get_qty(self, obj):
        return sum(i.quantity for i in obj.items.all())

    def get_unit_price(self, obj):
        items = obj.items.all()
        return sum(i.unit_price for i in items) / len(items) if items else 0

    def get_total(self, obj):
        return sum(i.total_cost for i in obj.items.all())

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