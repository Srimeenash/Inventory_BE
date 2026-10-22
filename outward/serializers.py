from uuid import uuid4

from django.utils import timezone
from rest_framework import serializers

from inventory.serial_display import (
    display_serial_metadata,
    display_serial_number,
    display_serial_text,
    get_inventory_code_id_map,
)
from inventory.cost_serializers import CostDetailsSerializerMixin

from .models import OutwardEntry


class OutwardEntrySerializer(CostDetailsSerializerMixin, serializers.ModelSerializer):
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

    material_request_number = serializers.CharField(
        source="material_request.material_request_id",
        read_only=True,
        default="",
    )

    code = serializers.CharField(
        required=False,
        read_only=True,
    )
    restore_workflow_status = serializers.SerializerMethodField()
    restore_po_number = serializers.SerializerMethodField()

    class Meta:
        model = OutwardEntry
        fields = "__all__"

        # Approval state is controlled by the backend workflow only.
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "inventory_allocations",
            "returned_serial_numbers",
            "stock_deducted",
            "stock_restored",
            "approval_status",
            "rejected_by",

            # Engineer Scrap staging fields are backend-owned.
            "source",
            "scrap_origin",
            "material_request",
            "requested_by",
            "requested_by_user_id",
            "moved_to_inventory",
            "moved_at",
        ]

    @staticmethod
    def _get_allocation_metadata(instance):
        metadata = getattr(instance, "inventory_allocations", {})
        if isinstance(metadata, dict):
            return metadata
        if isinstance(metadata, str):
            try:
                import json

                parsed = json.loads(metadata)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    @staticmethod
    def _qc_rows_quantity(rows):
        if not isinstance(rows, list):
            return 0

        total = 0
        for row in rows:
            if not isinstance(row, dict):
                total += 1
                continue
            raw_quantity = (
                row.get("qty")
                or row.get("quantity")
                or row.get("failed_quantity")
                or row.get("passed_quantity")
            )
            try:
                quantity = int(raw_quantity or 0)
            except (TypeError, ValueError):
                quantity = 0
            if quantity > 0:
                total += quantity
                continue
            serials = row.get("serial_numbers") or row.get("serialNumbers") or []
            total += len(serials) if isinstance(serials, list) and serials else 1
        return total

    def get_restore_workflow_status(self, instance):
        """Return the current state of Returnable QC Restore rows."""
        metadata = self._get_allocation_metadata(instance)
        workflow = str(metadata.get("workflow") or "").strip().upper()
        if workflow not in {
            "RETURNABLE_COMPONENT_QC_V1",
            "RETURNABLE_DRONE_QC_V1",
        }:
            return ""

        decision = str(
            metadata.get("manager_disposition_decision")
            or metadata.get("reorder_choice")
            or ""
        ).strip().upper()
        approval_status = str(
            getattr(instance, "approval_status", "") or ""
        ).strip().upper()
        if approval_status in {"REJECTED", "MANAGER_REJECTED", "FINANCE_REJECTED"}:
            return "RESTORE_REJECTED"

        restore_status = str(
            metadata.get("procurement_restore_status") or ""
        ).strip().upper()
        if decision == "NO" or restore_status == "NOT_REQUIRED":
            if approval_status == "APPROVED":
                return "FINAL_SCRAP_COMPLETED"
            if approval_status == "PENDING_FINANCE":
                return "FINAL_SCRAP_PENDING_FINANCE"
            return "FINAL_SCRAP"

        raw_po_ids = metadata.get("restore_po_ids")
        if not isinstance(raw_po_ids, list):
            raw_po_ids = []
        else:
            raw_po_ids = list(raw_po_ids)
        if metadata.get("restore_po_id") is not None:
            raw_po_ids.append(metadata["restore_po_id"])

        po_ids = []
        for raw_po_id in raw_po_ids:
            try:
                po_id = int(raw_po_id)
            except (TypeError, ValueError):
                continue
            if po_id > 0 and po_id not in po_ids:
                po_ids.append(po_id)

        if po_ids:
            # Import lazily so the outward serializer does not introduce an
            # import cycle with Procurement or Inward.
            from procurement.models import PurchaseOrder

            purchase_orders = list(
                PurchaseOrder.objects.filter(pk__in=po_ids)
                .only("id", "status")
            )
            if not purchase_orders:
                return "RESTORE_PO_RAISED"

            po_statuses = {
                str(purchase_order.status or "").strip().upper()
                for purchase_order in purchase_orders
            }
            if po_statuses.intersection(
                {"REJECTED", "FINANCE_REJECTED", "REPLACEMENT_FINANCE_REJECTED"}
            ):
                return "RESTORE_FINANCE_REJECTED"
            if po_statuses.intersection(
                {"PENDING_FINANCE", "REPLACEMENT_PENDING_FINANCE"}
            ):
                return "RESTORE_PENDING_FINANCE"

            received_statuses = {
                "REPLACEMENT_RECEIVED",
                "REPLACEMENT_PARTIALLY_RECEIVED",
            }
            if po_statuses.intersection(received_statuses):
                if (
                    "REPLACEMENT_PARTIALLY_RECEIVED" in po_statuses
                    or not po_statuses.issubset(received_statuses)
                ):
                    return "RESTORE_PARTIALLY_RECEIVED"

                from inward.models import InwardEntry

                inwards = list(
                    InwardEntry.objects.filter(
                        purchase_order_id__in=po_ids,
                    )
                    .exclude(removed_from_inventory=True)
                    .only(
                        "quantity_received",
                        "qc_status",
                        "qc_passed_rows",
                        "qc_failed_rows",
                    )
                )
                inspected_po_ids = {
                    inward.purchase_order_id
                    for inward in inwards
                }
                if not inwards or not set(po_ids).issubset(inspected_po_ids):
                    return "RESTORE_RECEIVED_QC_PENDING"

                all_inspected = True
                passed_quantity = 0
                failed_quantity = 0
                for inward in inwards:
                    passed = self._qc_rows_quantity(inward.qc_passed_rows)
                    failed = self._qc_rows_quantity(inward.qc_failed_rows)
                    passed_quantity += passed
                    failed_quantity += failed
                    inspected = passed + failed
                    if (
                        str(inward.qc_status or "").strip().upper()
                        not in {"COMPLETED", "PASS", "FAIL"}
                        or inspected != int(inward.quantity_received or 0)
                    ):
                        all_inspected = False

                if not all_inspected:
                    return "RESTORE_RECEIVED_QC_PENDING"
                if failed_quantity > 0:
                    return "RESTORE_QC_FAILED"
                if passed_quantity >= sum(
                    int(inward.quantity_received or 0)
                    for inward in inwards
                ):
                    return "RESTORE_COMPLETED"
                return "RESTORE_RECEIVED_QC_PENDING"

            if po_statuses.intersection(
                {"REPLACEMENT_ORDERED", "ORDERED"}
            ):
                return "RESTORE_ORDERED"
            if po_statuses.intersection(
                {"REPLACEMENT_APPROVED", "REPLACEMENT_FINANCE_APPROVED", "APPROVED"}
            ):
                return "RESTORE_FINANCE_APPROVED"
            return "RESTORE_PO_RAISED"

        if restore_status == "PENDING_PROCUREMENT":
            return "PENDING_PROCUREMENT"
        if restore_status == "PO_RAISED":
            return "RESTORE_PO_RAISED"
        if restore_status == "PENDING_FINANCE":
            return "RESTORE_PENDING_FINANCE"
        if restore_status == "FINANCE_APPROVED":
            return "RESTORE_FINANCE_APPROVED"
        if restore_status == "ORDERED":
            return "RESTORE_ORDERED"
        if restore_status == "FINANCE_REJECTED":
            return "RESTORE_FINANCE_REJECTED"
        if restore_status == "NOT_REQUIRED":
            return "FINAL_SCRAP"

        if approval_status == "PENDING_MANAGER":
            return "PENDING_MANAGER"
        if approval_status == "PENDING_FINANCE":
            return "PENDING_FINANCE"
        return "WAITING_MANAGER_FINANCE"

    def get_restore_po_number(self, instance):
        metadata = self._get_allocation_metadata(instance)
        return str(metadata.get("restore_po_number") or "").strip()

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
                getattr(
                    self.instance,
                    "outward_type",
                    "SCRAP",
                ),
            )
            or "SCRAP"
        ).strip().upper()

        item_type = str(
            attrs.get(
                "item_type",
                getattr(
                    self.instance,
                    "item_type",
                    "COMPONENT",
                ),
            )
            or "COMPONENT"
        ).strip().upper()

        if is_create or "outward_type" in attrs:
            attrs["outward_type"] = outward_type

        if is_create or "item_type" in attrs:
            attrs["item_type"] = item_type

        quantity_was_supplied = (
            "quantity" in attrs
            or "no_of_components" in attrs
        )

        raw_quantity = attrs.get(
            "quantity",
            attrs.get(
                "no_of_components",
                getattr(
                    self.instance,
                    "quantity",
                    1,
                ),
            ),
        )

        try:
            quantity = int(raw_quantity or 0)
        except (TypeError, ValueError):
            quantity = 0

        if quantity <= 0:
            raise serializers.ValidationError(
                {
                    "quantity":
                        "Quantity must be greater than zero."
                }
            )

        if is_create or quantity_was_supplied:
            attrs["quantity"] = quantity
            attrs["no_of_components"] = quantity

        if outward_type in {"SALES", "EVENT"}:
            component = attrs.get(
                "component",
                getattr(
                    self.instance,
                    "component",
                    None,
                ),
            )

            product_name = str(
                attrs.get(
                    "product_name",
                    getattr(
                        self.instance,
                        "product_name",
                        "",
                    ),
                )
                or ""
            ).strip()

            drone_name = str(
                attrs.get(
                    "drone_name",
                    getattr(
                        self.instance,
                        "drone_name",
                        "",
                    ),
                )
                or ""
            ).strip()

            if (
                item_type == "COMPONENT"
                and component is None
            ):
                raise serializers.ValidationError(
                    {
                        "component":
                            "Select an In-Store component."
                    }
                )

            if (
                item_type == "DRONE"
                and not (
                    product_name
                    or drone_name
                )
            ):
                raise serializers.ValidationError(
                    {
                        "product_name":
                            "Enter the drone name."
                    }
                )

        serials_were_supplied = (
            "serial_numbers" in attrs
        )

        serial_numbers = self.normalize_serials(
            attrs.get(
                "serial_numbers",
                getattr(
                    self.instance,
                    "serial_numbers",
                    [],
                ),
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
                        f"Select exactly {quantity} "
                        "serial number(s) for this component."
                    )
                }
            )

        return attrs

    def create(self, validated_data):
        if not validated_data.get("code"):
            stamp = timezone.now().strftime(
                "%Y%m%d%H%M%S%f"
            )

            validated_data["code"] = (
                f"OUT-{stamp}-"
                f"{uuid4().hex[:6].upper()}"
            )

        outward_type = str(
            validated_data.get(
                "outward_type",
                "SCRAP",
            )
            or "SCRAP"
        ).strip().upper()

        # ----------------------------------------------------
        # SCRAP approval workflow
        # ----------------------------------------------------
        # Every new Scrap starts with Manager approval:
        #
        # PENDING_MANAGER -> PENDING_FINANCE -> APPROVED
        #
        # Manager approval moves the Scrap to PENDING_FINANCE.
        # Finance approval is the final APPROVED state.
        # ----------------------------------------------------
        if outward_type == "SCRAP":
            validated_data["approval_status"] = "PENDING_MANAGER"
            validated_data["status"] = "PENDING_MANAGER"
            validated_data["rejection_reason"] = None
            validated_data["rejected_by"] = None

        else:
            validated_data[
                "approval_status"
            ] = "NOT_REQUESTED"

            if not validated_data.get("status"):
                validated_data["status"] = "NEW"

        return super().create(validated_data)

    def update(
        self,
        instance,
        validated_data,
    ):
        # IMPORTANT:
        # Never reset approval_status here.
        #
        # The old code forced every PATCH back to
        # NOT_REQUESTED, which breaks Scrap approval.
        return super().update(
            instance,
            validated_data,
        )

    def to_representation(
        self,
        instance,
    ):
        data = super().to_representation(
            instance
        )

        code_to_id = get_inventory_code_id_map(self.context)
        raw_serials = data.get("serial_numbers") or []
        data["display_serial_numbers"] = [
            display_serial_number(
                serial,
                code_to_id=code_to_id,
            )
            for serial in raw_serials
        ] if isinstance(raw_serials, list) else []

        raw_allocations = data.get("inventory_allocations") or {}
        data["display_inventory_allocations"] = display_serial_metadata(
            raw_allocations,
            code_to_id=code_to_id,
        )

        data["display_remarks"] = display_serial_text(
            data.get("remarks") or "",
            code_to_id=code_to_id,
        )

        data["outDate"] = data.get(
            "out_date"
        )
        data["productName"] = data.get(
            "product_name"
        )
        data["invoiceNumber"] = data.get(
            "invoice_number"
        )
        data["gatePass"] = data.get(
            "gate_pass"
        )
        data["eventName"] = data.get(
            "event_name"
        )
        data["noOfComponents"] = data.get(
            "no_of_components"
        )
        data["returnDate"] = data.get(
            "return_date"
        )
        data["droneName"] = data.get(
            "drone_name"
        )
        data["attendeeName"] = data.get(
            "attendee_name"
        )
        data["eventComponents"] = data.get(
            "event_components"
        )
        data["isReturned"] = data.get(
            "is_returned"
        )
        data["typeOfOutward"] = data.get(
            "outward_type"
        )
        data["itemType"] = data.get(
            "item_type"
        )
        data["serialNumbers"] = (
            data.get("serial_numbers")
            or []
        )
        data["returnedQuantity"] = (
            data.get("returned_quantity")
            or 0
        )
        data["returnedSerialNumbers"] = (
            data.get(
                "returned_serial_numbers"
            )
            or []
        )
        data["inventoryAllocations"] = (
            data.get(
                "inventory_allocations"
            )
            or []
        )
        data["displaySerialNumbers"] = data.get(
            "display_serial_numbers",
            [],
        )
        data["displayInventoryAllocations"] = data.get(
            "display_inventory_allocations",
            {},
        )
        data["displayRemarks"] = data.get(
            "display_remarks",
            "",
        )
        data["stockDeducted"] = bool(
            data.get("stock_deducted")
        )
        data["stockRestored"] = bool(
            data.get("stock_restored")
        )

        data["scrapOrigin"] = (
            data.get("scrap_origin")
            or "OTHER"
        )
        data["materialRequestNumber"] = (
            data.get("material_request_number")
            or ""
        )

        return data
