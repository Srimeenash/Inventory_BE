from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_HALF_UP,
)

from rest_framework import serializers

from components.models import Component
from procurement.models import PurchaseOrder

from .models import (
    InwardEntry,
    InwardLineItem,
)


def get_qc_row_quantity(row):
    if not isinstance(row, dict):
        return 0

    raw_value = (
        row.get("qty")
        if row.get("qty") is not None
        else row.get("quantity")
        if row.get("quantity") is not None
        else row.get("passed_quantity")
        if row.get("passed_quantity") is not None
        else row.get("failed_quantity")
        if row.get("failed_quantity") is not None
        else 1
    )

    try:
        return max(int(raw_value), 0)
    except (TypeError, ValueError):
        return 0


def get_qc_rows_quantity(rows):
    return sum(
        get_qc_row_quantity(row)
        for row in (rows or [])
    )


class FlexibleComponentRelatedField(
    serializers.PrimaryKeyRelatedField
):
    def to_internal_value(self, data):
        if data is None or data == "":
            self.fail("required")

        if isinstance(data, str):
            value = data.strip()

            if "—" in value:
                value = (
                    value.split("—", 1)[0]
                    .strip()
                )
            elif ":" in value:
                value = (
                    value.split(":", 1)[0]
                    .strip()
                )
            elif (
                "-" in value
                and not value.startswith("CMP-")
            ):
                value = (
                    value.split("-", 1)[0]
                    .strip()
                )

            if value.isdigit():
                try:
                    return Component.objects.get(
                        pk=int(value)
                    )
                except Component.DoesNotExist:
                    pass

            try:
                return Component.objects.get(
                    component_id=value
                )
            except Component.DoesNotExist:
                pass

            try:
                return Component.objects.get(
                    name=value
                )
            except Component.DoesNotExist:
                pass

        try:
            return super().to_internal_value(
                data
            )
        except serializers.ValidationError:
            raise serializers.ValidationError(
                "Invalid component reference."
            )


class FlexiblePurchaseOrderRelatedField(
    serializers.PrimaryKeyRelatedField
):
    def to_internal_value(self, data):
        if data is None or data == "":
            return None

        if isinstance(data, str):
            value = data.strip()

            # Only remove display suffixes.
            # Do not split on "-", because the valid PO formats
            # contain financial-year hyphens:
            #
            # 01/26-27
            # 03/26-27_01
            if "—" in value:
                value = (
                    value.split("—", 1)[0]
                    .strip()
                )
            elif ":" in value:
                value = (
                    value.split(":", 1)[0]
                    .strip()
                )

            if value.isdigit():
                try:
                    return PurchaseOrder.objects.get(
                        pk=int(value)
                    )
                except PurchaseOrder.DoesNotExist:
                    pass

            try:
                return PurchaseOrder.objects.get(
                    po_number=value
                )
            except PurchaseOrder.DoesNotExist:
                pass

        try:
            return super().to_internal_value(
                data
            )
        except serializers.ValidationError:
            raise serializers.ValidationError(
                "Invalid purchase order reference."
            )


def normalize_decimal_input(
    value,
    *,
    decimal_places=2,
):
    """
    Normalize a decimal-compatible value before DRF DecimalField
    validates the number of decimal places.

    Invalid values are returned unchanged so DRF can produce its normal
    field validation error.
    """
    if value in (None, ""):
        return value

    try:
        decimal_value = Decimal(str(value))
    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ):
        return value

    quantum = Decimal("1").scaleb(
        -int(decimal_places)
    )

    return format(
        decimal_value.quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        ),
        f".{int(decimal_places)}f",
    )


class InwardLineItemSerializer(
    serializers.ModelSerializer
):
    inward_entry = (
        serializers.PrimaryKeyRelatedField(
            read_only=True
        )
    )

    invoiceNo = serializers.CharField(
        source="invoice_number",
        write_only=True,
        required=False,
        allow_blank=True,
    )

    invoiceDate = serializers.DateField(
        source="invoice_date",
        write_only=True,
        required=False,
        allow_null=True,
    )

    totalQty = serializers.IntegerField(
        source="total_quantity",
        write_only=True,
        required=False,
        allow_null=True,
    )

    gst = serializers.DecimalField(
        source="gst_percentage",
        max_digits=5,
        decimal_places=2,
        write_only=True,
        required=False,
        allow_null=True,
    )

    grandTotal = serializers.DecimalField(
        source="grand_total",
        max_digits=18,
        decimal_places=2,
        write_only=True,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = InwardLineItem

        fields = [
            "id",
            "inward_entry",
            "specification",
            "invoice_number",
            "invoice_date",
            "total_quantity",
            "quantity",
            "unit_price",
            "gst_percentage",
            "grand_total",
            "invoiceNo",
            "invoiceDate",
            "totalQty",
            "gst",
            "grandTotal",
        ]

        read_only_fields = [
            "id",
            "inward_entry",
        ]

    def to_internal_value(self, data):
        """
        Accept safe decimal inputs from all Inward frontends.

        JavaScript can produce values such as:
            24888.000000000004

        Normalize supported decimal fields before ModelSerializer's
        DecimalField validation checks decimal_places.
        """
        if hasattr(data, "copy"):
            normalized_data = data.copy()
        else:
            normalized_data = dict(data or {})

        decimal_fields = {
            "unit_price": 2,
            "gst_percentage": 2,
            "grand_total": 2,
            "gst": 2,
            "grandTotal": 2,
        }

        for field_name, decimal_places in (
            decimal_fields.items()
        ):
            if field_name in normalized_data:
                normalized_data[field_name] = (
                    normalize_decimal_input(
                        normalized_data[field_name],
                        decimal_places=decimal_places,
                    )
                )

        return super().to_internal_value(
            normalized_data
        )

    def to_representation(self, instance):
        data = super().to_representation(
            instance
        )

        data["invoiceNo"] = data.get(
            "invoice_number"
        )
        data["invoiceDate"] = data.get(
            "invoice_date"
        )
        data["totalQty"] = data.get(
            "total_quantity"
        )
        data["gst"] = data.get(
            "gst_percentage"
        )
        data["grandTotal"] = data.get(
            "grand_total"
        )

        return data


class InwardEntrySerializer(
    serializers.ModelSerializer
):
    component = FlexibleComponentRelatedField(
        queryset=Component.objects.all()
    )

    purchase_order = (
        FlexiblePurchaseOrderRelatedField(
            queryset=
                PurchaseOrder.objects.all(),
            required=False,
            allow_null=True,
        )
    )

    po = FlexiblePurchaseOrderRelatedField(
        queryset=PurchaseOrder.objects.all(),
        source="purchase_order",
        write_only=True,
        required=False,
        allow_null=True,
    )

    date = serializers.DateField(
        source="received_date",
        write_only=True,
        required=False,
    )

    receivedDate = serializers.DateField(
        source="received_date",
        write_only=True,
        required=False,
    )

    batchNumber = serializers.CharField(
        source="batch_number",
        write_only=True,
        required=False,
        allow_blank=True,
    )

    quantity = serializers.IntegerField(
        source="quantity_received",
        write_only=True,
        required=False,
    )

    items = serializers.IntegerField(
        source="quantity_received",
        write_only=True,
        required=False,
    )

    qc = serializers.CharField(
        source="qc_status",
        write_only=True,
        required=False,
        allow_blank=True,
    )

    passedRows = serializers.JSONField(
        source="qc_passed_rows",
        read_only=True,
    )

    failedRows = serializers.JSONField(
        source="qc_failed_rows",
        read_only=True,
    )

    qcTimestamp = serializers.DateTimeField(
        source="qc_timestamp",
        read_only=True,
    )

    component_code = serializers.CharField(
        source="component.component_id",
        read_only=True,
        default="",
    )

    component_name = serializers.CharField(
        source="component.name",
        read_only=True,
        default="",
    )

    purchase_order_number = (
        serializers.SerializerMethodField()
    )

    purchase_order_status = (
        serializers.SerializerMethodField()
    )

    replacement_purchase_order_number = (
        serializers.SerializerMethodField()
    )

    replacement_purchase_order_status = (
        serializers.SerializerMethodField()
    )

    qc_passed_count = (
        serializers.SerializerMethodField()
    )

    qc_failed_count = (
        serializers.SerializerMethodField()
    )

    source_mr_number = (
        serializers.SerializerMethodField()
    )

    line_items = InwardLineItemSerializer(
        many=True,
        required=False,
    )

    class Meta:
        model = InwardEntry
        fields = "__all__"

        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "qc_passed_count",
            "qc_failed_count",
            "source_mr_number",
            "component_code",
            "component_name",
            "purchase_order_number",
            "purchase_order_status",
            "qc_failed_action",
            "replacement_purchase_order",
            "replacement_purchase_order_number",
            "replacement_purchase_order_status",
        ]

    def get_qc_passed_count(self, obj):
        return get_qc_rows_quantity(
            obj.qc_passed_rows
        )

    def get_qc_failed_count(self, obj):
        return get_qc_rows_quantity(
            obj.qc_failed_rows
        )

    def get_source_mr_number(self, obj):
        if not obj.purchase_order:
            return ""

        return str(
            obj.purchase_order
            .source_mr_number
            or ""
        ).strip()

    def get_purchase_order_number(self, obj):
        if not obj.purchase_order:
            return ""

        return str(
            obj.purchase_order.po_number
            or ""
        ).strip()

    def get_purchase_order_status(self, obj):
        if not obj.purchase_order:
            return ""

        return str(
            obj.purchase_order.status
            or ""
        ).strip()

    def get_replacement_purchase_order_number(
        self,
        obj,
    ):
        replacement_po = getattr(
            obj,
            "replacement_purchase_order",
            None,
        )

        if not replacement_po:
            return ""

        return str(
            replacement_po.po_number
            or ""
        ).strip()

    def get_replacement_purchase_order_status(
        self,
        obj,
    ):
        replacement_po = getattr(
            obj,
            "replacement_purchase_order",
            None,
        )

        if not replacement_po:
            return ""

        return str(
            replacement_po.status
            or ""
        ).strip()

    def create(self, validated_data):
        line_items_data = validated_data.pop(
            "line_items",
            [],
        )

        inward_entry = (
            InwardEntry.objects.create(
                **validated_data
            )
        )

        for item_data in line_items_data:
            item_data.pop(
                "inward_entry",
                None,
            )

            InwardLineItem.objects.create(
                inward_entry=inward_entry,
                **item_data,
            )

        return inward_entry

    def update(
        self,
        instance,
        validated_data,
    ):
        line_items_data = validated_data.pop(
            "line_items",
            None,
        )

        for attr, value in (
            validated_data.items()
        ):
            setattr(
                instance,
                attr,
                value,
            )

        instance.save()

        if line_items_data is not None:
            instance.line_items.all().delete()

            for item_data in line_items_data:
                item_data.pop(
                    "inward_entry",
                    None,
                )

                InwardLineItem.objects.create(
                    inward_entry=instance,
                    **item_data,
                )

        return instance