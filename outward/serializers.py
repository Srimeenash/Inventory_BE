from uuid import uuid4

from django.utils import timezone
from rest_framework import serializers

from .models import OutwardEntry


class OutwardEntrySerializer(serializers.ModelSerializer):
    typeOfOutward = serializers.ChoiceField(
        choices=OutwardEntry.OUTWARD_TYPE_CHOICES,
        source="outward_type",
        write_only=True,
        required=False,
        allow_null=True,
    )
    itemType = serializers.ChoiceField(
        choices=OutwardEntry.ITEM_TYPE_CHOICES,
        source="item_type",
        write_only=True,
        required=False,
        allow_null=True,
    )
    outDate = serializers.DateField(
        source="out_date",
        write_only=True,
        required=False,
        allow_null=True,
    )
    productName = serializers.CharField(
        source="product_name",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    invoiceNumber = serializers.CharField(
        source="invoice_number",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    gatePass = serializers.CharField(
        source="gate_pass",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    eventName = serializers.CharField(
        source="event_name",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    noOfComponents = serializers.IntegerField(
        source="no_of_components",
        write_only=True,
        required=False,
        allow_null=True,
    )
    returnDate = serializers.DateField(
        source="return_date",
        write_only=True,
        required=False,
        allow_null=True,
    )
    droneName = serializers.CharField(
        source="drone_name",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    attendeeName = serializers.CharField(
        source="attendee_name",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    eventComponents = serializers.CharField(
        source="event_components",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )
    isReturned = serializers.BooleanField(
        source="is_returned",
        write_only=True,
        required=False,
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

    code = serializers.CharField(required=False, read_only=True)

    class Meta:
        model = OutwardEntry
        fields = "__all__"
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "inventory_allocations",
            "returned_serial_numbers",
            "stock_deducted",
            "stock_restored",
            "approval_status",
        ]

    @staticmethod
    def normalize_serials(values):
        if not isinstance(values, list):
            return []

        result = []
        seen = set()
        for value in values:
            serial = str(value or "").strip()
            if serial and serial not in seen:
                seen.add(serial)
                result.append(serial)
        return result

    def validate(self, attrs):
        attrs = super().validate(attrs)

        is_create = self.instance is None

        outward_type = str(
            attrs.get(
                "outward_type",
                getattr(self.instance, "outward_type", "SCRAP"),
            )
            or "SCRAP"
        ).strip().upper()

        item_type = str(
            attrs.get(
                "item_type",
                getattr(self.instance, "item_type", "COMPONENT"),
            )
            or "COMPONENT"
        ).strip().upper()

        # On PATCH, do not inject immutable fields that were not supplied.
        # The stock-aware ViewSet protects these fields after stock movement.
        if is_create or "outward_type" in attrs:
            attrs["outward_type"] = outward_type

        if is_create or "item_type" in attrs:
            attrs["item_type"] = item_type

        quantity_was_supplied = (
            "quantity" in attrs or "no_of_components" in attrs
        )

        raw_quantity = attrs.get(
            "quantity",
            attrs.get(
                "no_of_components",
                getattr(self.instance, "quantity", 1),
            ),
        )

        try:
            quantity = int(raw_quantity or 0)
        except (TypeError, ValueError):
            quantity = 0

        if quantity <= 0:
            raise serializers.ValidationError(
                {"quantity": "Quantity must be greater than zero."}
            )

        if is_create or quantity_was_supplied:
            attrs["quantity"] = quantity
            attrs["no_of_components"] = quantity

        if outward_type in {"SALES", "EVENT"}:
            component = attrs.get(
                "component",
                getattr(self.instance, "component", None),
            )
            product_name = str(
                attrs.get(
                    "product_name",
                    getattr(self.instance, "product_name", ""),
                )
                or ""
            ).strip()
            drone_name = str(
                attrs.get(
                    "drone_name",
                    getattr(self.instance, "drone_name", ""),
                )
                or ""
            ).strip()

            if item_type == "COMPONENT" and component is None:
                raise serializers.ValidationError(
                    {"component": "Select an In-Store component."}
                )

            if item_type == "DRONE" and not (product_name or drone_name):
                raise serializers.ValidationError(
                    {"product_name": "Enter the drone name."}
                )

        serials_were_supplied = "serial_numbers" in attrs
        serial_numbers = self.normalize_serials(
            attrs.get(
                "serial_numbers",
                getattr(self.instance, "serial_numbers", []),
            )
        )

        if is_create or serials_were_supplied:
            attrs["serial_numbers"] = serial_numbers

        if (
            outward_type in {"SALES", "EVENT"}
            and item_type == "COMPONENT"
            and is_create
            and len(serial_numbers) != quantity
        ):
            raise serializers.ValidationError(
                {
                    "serial_numbers": (
                        f"Select exactly {quantity} serial number(s) "
                        "for this component."
                    )
                }
            )

        return attrs

    def create(self, validated_data):
        if not validated_data.get("code"):
            stamp = timezone.now().strftime("%Y%m%d%H%M%S%f")
            validated_data["code"] = (
                f"OUT-{stamp}-{uuid4().hex[:6].upper()}"
            )

        if not validated_data.get("status"):
            validated_data["status"] = "NEW"

        validated_data["approval_status"] = "NOT_REQUESTED"
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # This workflow has no approval step.
        validated_data["approval_status"] = "NOT_REQUESTED"
        return super().update(instance, validated_data)

    def to_representation(self, instance):
        data = super().to_representation(instance)

        data["outDate"] = data.get("out_date")
        data["productName"] = data.get("product_name")
        data["invoiceNumber"] = data.get("invoice_number")
        data["gatePass"] = data.get("gate_pass")
        data["eventName"] = data.get("event_name")
        data["noOfComponents"] = data.get("no_of_components")
        data["returnDate"] = data.get("return_date")
        data["droneName"] = data.get("drone_name")
        data["attendeeName"] = data.get("attendee_name")
        data["eventComponents"] = data.get("event_components")
        data["isReturned"] = data.get("is_returned")
        data["typeOfOutward"] = data.get("outward_type")
        data["itemType"] = data.get("item_type")
        data["serialNumbers"] = data.get("serial_numbers") or []
        data["returnedQuantity"] = data.get("returned_quantity") or 0
        data["returnedSerialNumbers"] = (
            data.get("returned_serial_numbers") or []
        )
        data["inventoryAllocations"] = (
            data.get("inventory_allocations") or []
        )
        data["stockDeducted"] = bool(data.get("stock_deducted"))
        data["stockRestored"] = bool(data.get("stock_restored"))

        return data