from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from components.models import Component
from notifications.models import Notification
from vendors.models import Vendor, VendorProduct

from .models import (
    PurchaseOrder,
    PurchaseOrderApproval,
    PurchaseOrderItem,
    PurchaseRequest,
    PurchaseRequestItem,
)


# ---------------- COMPONENT ----------------
class ComponentMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Component
        fields = [
            "id",
            "component_id",
            "name",
            "component_type",
            "hsn_numbers",
        ]


# ---------------- PURCHASE REQUEST ITEM ----------------
class PurchaseRequestItemSerializer(serializers.ModelSerializer):
    component = ComponentMiniSerializer(read_only=True)

    class Meta:
        model = PurchaseRequestItem
        fields = ["id", "component", "quantity", "remarks"]


# ---------------- PURCHASE REQUEST ----------------
class PurchaseRequestSerializer(
    serializers.ModelSerializer
):
    items = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseRequest
        fields = "__all__"

    def _get_request_items_cache(self, obj):
        cache = self.context.get(
            "_purchase_request_items_cache"
        )

        if cache is not None:
            return cache

        root_instance = getattr(
            self.root,
            "instance",
            None,
        )

        if root_instance is None:
            request_ids = [obj.pk]
        else:
            try:
                root_rows = list(root_instance)
            except TypeError:
                root_rows = [root_instance]

            request_ids = [
                row.pk
                for row in root_rows
                if getattr(row, "pk", None)
            ]

            if obj.pk not in request_ids:
                request_ids.append(obj.pk)

        grouped = {
            request_id: []
            for request_id in request_ids
        }

        rows = (
            PurchaseRequestItem.objects
            .select_related("component")
            .filter(
                purchase_request_id__in=
                    request_ids
            )
            .order_by(
                "purchase_request_id",
                "id",
            )
        )

        for row in rows:
            grouped.setdefault(
                row.purchase_request_id,
                [],
            ).append(row)

        self.context[
            "_purchase_request_items_cache"
        ] = grouped

        return grouped

    def get_items(self, obj):
        rows = self._get_request_items_cache(
            obj
        ).get(
            obj.pk,
            [],
        )

        return PurchaseRequestItemSerializer(
            rows,
            many=True,
            context=self.context,
        ).data


# ---------------- PURCHASE ORDER ITEM ----------------
class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    component = ComponentMiniSerializer(
        read_only=True,
    )

    component_id = serializers.PrimaryKeyRelatedField(
        source="component",
        queryset=Component.objects.all(),
        write_only=True,
    )

    received_quantity = serializers.IntegerField(
        read_only=True,
    )

    remaining_quantity = serializers.IntegerField(
        read_only=True,
    )

    gst_percentage = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=0,
        max_value=100,
    )

    freight_gst_percentage = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        min_value=0,
        max_value=100,
    )

    subtotal = serializers.ReadOnlyField()
    taxable_amount = serializers.ReadOnlyField()
    gst_amount = serializers.ReadOnlyField()
    freight_gst_amount = serializers.ReadOnlyField()
    total_cost = serializers.ReadOnlyField()

    class Meta:
        model = PurchaseOrderItem

        fields = [
            "id",
            "component",
            "component_id",
            "quantity",
            "uom",
            "hsn_no",
            "received_quantity",
            "remaining_quantity",
            "unit_price",
            "discount",
            "gst_percentage",
            "gst_amount",
            "freight_cost",
            "freight_gst_percentage",
            "freight_gst_amount",
            "expected_delivery_date",
            "subtotal",
            "taxable_amount",
            "total_cost",
        ]

        read_only_fields = [
            "id",
            "received_quantity",
            "remaining_quantity",
            "subtotal",
            "taxable_amount",
            "gst_amount",
            "freight_gst_amount",
            "total_cost",
        ]

    def validate_gst_percentage(self, value):
        if (
            value is not None
            and not 0 <= value <= 100
        ):
            raise serializers.ValidationError(
                "GST percentage must be between 0 and 100."
            )

        return value

    def validate_discount(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError(
                "Discount cannot be negative."
            )
        return value

    def validate_freight_cost(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError(
                "Freight Cost cannot be negative."
            )
        return value

    def validate_freight_gst_percentage(self, value):
        if (
            value is not None
            and not 0 <= value <= 100
        ):
            raise serializers.ValidationError(
                "Freight GST percentage must be between 0 and 100."
            )
        return value

    def _apply_component_snapshot_defaults(
        self,
        validated_data,
    ):
        component = validated_data.get("component")

        if component and not str(
            validated_data.get("hsn_no") or ""
        ).strip():
            validated_data["hsn_no"] = str(
                getattr(component, "hsn_numbers", "")
                or ""
            ).strip()

        # UOM is NOT copied from Component Master.
        # For MR-generated POs the frontend sends MR/BOM UOM.
        # For Direct PO Procurement may type UOM manually.
        validated_data["uom"] = str(
            validated_data.get("uom") or ""
        ).strip()

        return validated_data

    def create(self, validated_data):
        return super().create(
            self._apply_component_snapshot_defaults(
                validated_data
            )
        )

    def update(self, instance, validated_data):
        return super().update(
            instance,
            self._apply_component_snapshot_defaults(
                validated_data
            )
        )


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
    items_total = serializers.ReadOnlyField()
    grand_total = serializers.ReadOnlyField()

    total_received_quantity = (
        serializers.SerializerMethodField()
    )

    total_remaining_quantity = (
        serializers.SerializerMethodField()
    )

    latest_approval = (
        serializers.SerializerMethodField()
    )

    replacement_for_po_number = (
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
            "round_off",
            "remarks",
            "finance_remarks",
            "status",
            "approval_status",
            "latest_approval",
            "source_mr_number",
            "order_type",
            "replacement_for",
            "replacement_for_po_number",
            "replacement_round",
            "replacement_source_inward_id",
            "items",
            "deleted_items",
            "qty",
            "unit_price",
            "total",
            "items_total",
            "grand_total",
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
            "items_total",
            "grand_total",
            "total_received_quantity",
            "total_remaining_quantity",
            "latest_approval",
            "order_type",
            "replacement_for",
            "replacement_for_po_number",
            "replacement_round",
            "replacement_source_inward_id",
            "created_at",
        ]

    # --------------------------------------------------
    # Calculated fields
    # --------------------------------------------------
    def _root_purchase_orders(self, obj):
        root_instance = getattr(
            self.root,
            "instance",
            None,
        )

        if root_instance is None:
            return [obj]

        try:
            rows = list(root_instance)
        except TypeError:
            rows = [root_instance]

        if obj not in rows:
            rows.append(obj)

        return [
            row
            for row in rows
            if isinstance(row, PurchaseOrder)
        ]

    def _get_latest_approval_cached(
        self,
        obj,
    ):
        cache = self.context.get(
            "_po_latest_approval_cache"
        )

        if cache is None:
            purchase_orders = (
                self._root_purchase_orders(obj)
            )

            ids = [
                row.pk
                for row in purchase_orders
                if getattr(row, "pk", None)
            ]

            cache = {}

            if ids:
                approvals = (
                    PurchaseOrderApproval.objects
                    .filter(
                        purchase_order_id__in=ids
                    )
                    .order_by(
                        "purchase_order_id",
                        "-created_at",
                        "-id",
                    )
                )

                for approval in approvals:
                    cache.setdefault(
                        approval.purchase_order_id,
                        approval,
                    )

            self.context[
                "_po_latest_approval_cache"
            ] = cache

        return cache.get(obj.pk)

    def _get_replacement_for_cached(
        self,
        obj,
    ):
        replacement_id = getattr(
            obj,
            "replacement_for_id",
            None,
        )

        if not replacement_id:
            return None

        fields_cache = getattr(
            getattr(obj, "_state", None),
            "fields_cache",
            {},
        )

        if "replacement_for" in fields_cache:
            return fields_cache[
                "replacement_for"
            ]

        cache = self.context.get(
            "_po_replacement_for_cache"
        )

        if cache is None:
            purchase_orders = (
                self._root_purchase_orders(obj)
            )

            replacement_ids = {
                row.replacement_for_id
                for row in purchase_orders
                if getattr(
                    row,
                    "replacement_for_id",
                    None,
                )
            }

            cache = (
                PurchaseOrder.objects
                .in_bulk(replacement_ids)
                if replacement_ids
                else {}
            )

            self.context[
                "_po_replacement_for_cache"
            ] = cache

        return cache.get(replacement_id)

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
                item.unit_price
                or Decimal("0")
                for item in items
            ),
            Decimal("0"),
        )

        return total_price / len(items)

    def get_total(self, obj):
        return (
            obj.grand_total
            if hasattr(obj, "grand_total")
            else sum(
                (
                    item.total_cost
                    or Decimal("0")
                    for item in obj.items.all()
                ),
                Decimal("0"),
            )
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

    def get_replacement_for_po_number(
        self,
        obj,
    ):
        replacement_for = (
            self._get_replacement_for_cached(
                obj
            )
        )

        if not replacement_for:
            return ""

        return str(
            replacement_for.po_number
            or ""
        ).strip()

    def get_latest_approval(self, obj):
        latest = (
            self._get_latest_approval_cached(
                obj
            )
        )

        if not latest:
            return None

        return {
            "id": latest.id,
            "action": latest.action,
            "requested_by":
                latest.requested_by,
            "approved_by":
                latest.approved_by,
            "finance_remarks":
                latest.finance_remarks,
            "created_at":
                latest.created_at,
        }

    # --------------------------------------------------
    # NEW: Sync PO component names into Vendor Details
    # --------------------------------------------------
    def sync_vendor_components(self, purchase_order):
        """
        Automatically sync components used in a Purchase Order
        into the selected Vendor's component list.

        Prevents duplicate component names and safely copies
        component version when available.
        """

        vendor_name = str(
            purchase_order.vendor_name or ""
        ).strip()

        if not vendor_name:
            return

        vendor = (
            Vendor.objects
            .filter(
                name__iexact=vendor_name,
                is_active=True,
            )
            .first()
        )

        if not vendor:
            return

        po_items = (
            purchase_order.items
            .select_related("component")
            .all()
        )

        for po_item in po_items:

            component = getattr(
                po_item,
                "component",
                None,
            )

            if not component:
                continue

            # -----------------------------
            # Component name
            # -----------------------------
            component_name = str(
                getattr(component, "name", "")
                or getattr(
                    component,
                    "component_name",
                    "",
                )
                or ""
            ).strip()

            if not component_name:
                continue

            # -----------------------------
            # Component version
            # IMPORTANT:
            # Always initialise this variable.
            # -----------------------------
            component_version = str(
                getattr(component, "version", "")
                or getattr(
                    component,
                    "component_version",
                    "",
                )
                or ""
            ).strip()

            # -----------------------------
            # Check whether this component
            # already exists for vendor
            # -----------------------------
            existing_product = (
                VendorProduct.objects
                .filter(
                    vendor=vendor,
                    product__iexact=component_name,
                )
                .first()
            )

            if existing_product:

                # Update version only when a version exists
                if (
                    component_version
                    and existing_product.product_version
                    != component_version
                ):
                    existing_product.product_version = (
                        component_version
                    )

                    existing_product.save(
                        update_fields=[
                            "product_version"
                        ]
                    )

                continue

            # -----------------------------
            # New vendor component
            # -----------------------------
            VendorProduct.objects.create(
                vendor=vendor,
                product=component_name,
                product_version=(
                    component_version
                    if component_version
                    else None
                ),
            )
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
            component = item_data.get("component")

            if (
                component
                and not str(
                    item_data.get("hsn_no") or ""
                ).strip()
            ):
                item_data["hsn_no"] = str(
                    getattr(
                        component,
                        "hsn_numbers",
                        "",
                    )
                    or ""
                ).strip()

            item_data["uom"] = str(
                item_data.get("uom") or ""
            ).strip()

            PurchaseOrderItem.objects.create(
                purchase_order=purchase_order,
                **item_data,
            )

        # NEW:
        # Automatically add any new PO component names
        # into the selected vendor's component list.
        self.sync_vendor_components(
            purchase_order
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

        # Normal status-only PATCH:
        # don't modify PO items.
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
                not existing_item
                and component
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

                existing_item.uom = str(
                    item_data.get(
                        "uom",
                        existing_item.uom,
                    )
                    or ""
                ).strip()

                existing_item.hsn_no = str(
                    item_data.get(
                        "hsn_no",
                        existing_item.hsn_no,
                    )
                    or ""
                ).strip()

                existing_item.unit_price = (
                    item_data.get(
                        "unit_price",
                        existing_item.unit_price,
                    )
                )

                existing_item.discount = (
                    item_data.get(
                        "discount",
                        existing_item.discount,
                    )
                )

                existing_item.gst_percentage = (
                    item_data.get(
                        "gst_percentage",
                        existing_item.gst_percentage,
                    )
                )

                existing_item.freight_cost = (
                    item_data.get(
                        "freight_cost",
                        existing_item.freight_cost,
                    )
                )

                existing_item.freight_gst_percentage = (
                    item_data.get(
                        "freight_gst_percentage",
                        existing_item.freight_gst_percentage,
                    )
                )

                existing_item.expected_delivery_date = (
                    item_data.get(
                        "expected_delivery_date",
                        existing_item.expected_delivery_date,
                    )
                )

                existing_item.save(
                    update_fields=[
                        "quantity",
                        "uom",
                        "hsn_no",
                        "unit_price",
                        "discount",
                        "gst_percentage",
                        "freight_cost",
                        "freight_gst_percentage",
                        "expected_delivery_date",
                    ]
                )

            else:
                if (
                    component
                    and not str(
                        item_data.get("hsn_no") or ""
                    ).strip()
                ):
                    item_data["hsn_no"] = str(
                        getattr(
                            component,
                            "hsn_numbers",
                            "",
                        )
                        or ""
                    ).strip()

                item_data["uom"] = str(
                    item_data.get("uom") or ""
                ).strip()

                PurchaseOrderItem.objects.create(
                    purchase_order=instance,
                    **item_data,
                )

        # NEW:
        # If an existing PO gets new components,
        # add those component names to Vendor Details too.
        self.sync_vendor_components(
            instance
        )

        return instance