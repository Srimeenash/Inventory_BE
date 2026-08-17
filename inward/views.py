from collections import defaultdict

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import (
    Inventory,
    InventoryReservation,
    ProjectInventory,
)
from materialrequest.models import MaterialRequest
from notifications.email_service import send_ipms_email
from notifications.models import Notification
from procurement.models import (
    PurchaseOrder,
    PurchaseOrderItem,
)

from .models import InwardEntry
from .serializers import InwardEntrySerializer


User = get_user_model()


class InwardQCSerializer(serializers.Serializer):
    passedRows = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        default=list,
    )

    failedRows = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        default=list,
    )

    timestamp = serializers.DateTimeField(
        required=False,
        allow_null=True,
    )

    remarks = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    @staticmethod
    def get_row_quantity(row):
        """
        Read one QC row quantity.

        Existing frontends may send:
        qty, quantity, passed_quantity or failed_quantity.

        When no quantity field is supplied, one row represents
        one inspected component.
        """

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
            quantity = int(raw_value)
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                "Every QC row quantity must be a valid whole number."
            )

        if quantity <= 0:
            raise serializers.ValidationError(
                "Every QC row quantity must be greater than zero."
            )

        return quantity

    @classmethod
    def normalize_rows(cls, rows, label):
        normalized_rows = []

        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise serializers.ValidationError(
                    {
                        label: [
                            f"Row {index + 1} must be an object."
                        ]
                    }
                )

            quantity = cls.get_row_quantity(row)

            remarks = str(
                row.get("remarks")
                or row.get("remark")
                or row.get("reason")
                or row.get("failure_reason")
                or row.get("qc_remarks")
                or ""
            ).strip()

            # Remarks are mandatory for both Pass and Fail.
            if not remarks:
                raise serializers.ValidationError(
                    {
                        label: [
                            f"Remarks are required for row {index + 1}."
                        ]
                    }
                )

            normalized_row = dict(row)
            normalized_row["qty"] = quantity
            normalized_row["remarks"] = remarks
            normalized_rows.append(normalized_row)

        return normalized_rows

    def validate(self, attrs):
        attrs["passedRows"] = self.normalize_rows(
            attrs.get("passedRows", []),
            "passedRows",
        )

        attrs["failedRows"] = self.normalize_rows(
            attrs.get("failedRows", []),
            "failedRows",
        )

        seen_serials = set()
        for label in ("passedRows", "failedRows"):
            for index, row in enumerate(attrs[label]):
                serial = str(
                    row.get("serialNumber")
                    or row.get("serial_number")
                    or row.get("serial")
                    or ""
                ).strip()
                if not serial:
                    raise serializers.ValidationError(
                        {label: [f"Serial number is required for row {index + 1}."]}
                    )
                if serial in seen_serials:
                    raise serializers.ValidationError(
                        {label: [f"Duplicate serial number: {serial}."]}
                    )
                seen_serials.add(serial)
                row["serialNumber"] = serial
                row["serial_number"] = serial

        return attrs


class InwardEntryViewSet(viewsets.ModelViewSet):
    serializer_class = InwardEntrySerializer

    # ==========================================================
    # INVENTORY EMAIL - QC PASSED COMPONENTS READY
    # ==========================================================

    @staticmethod
    def get_user_display_name(
        user,
        fallback="User",
    ):
        if not user:
            return fallback

        return (
            getattr(
                user,
                "employee_name",
                "",
            )
            or getattr(
                user,
                "email",
                "",
            )
            or fallback
        )

    @staticmethod
    def get_ipms_base_url():
        return str(
            getattr(
                settings,
                "IPMS_BASE_URL",
                "http://localhost:5173",
            )
            or "http://localhost:5173"
        ).rstrip("/")

    def send_inventory_qc_ready_email(
        self,
        material_request_id,
        *,
        workflow_complete=False,
    ):
        """
        Email Inventory when MR-linked QC makes components available
        for 'Provide Components'.

        Supports BOTH:
        - Partial QC: one component can be provided while other
          components are still awaiting delivery/QC.
        - Complete QC: the complete MR QC stage is ready for Inventory.

        The caller only invokes this when new QC-passed purchased stock
        becomes available, or when the MR enters the final QC-ready state.
        """
        try:
            material_request = (
                MaterialRequest.objects
                .select_related("requester")
                .get(pk=material_request_id)
            )
        except MaterialRequest.DoesNotExist:
            return False

        inventory_users = (
            User.objects
            .filter(
                role__iexact="inventory",
                is_active=True,
            )
            .exclude(email__isnull=True)
            .exclude(email="")
            .order_by("id")
        )

        if not inventory_users.exists():
            print(
                "QC INVENTORY EMAIL SKIPPED:",
                material_request.material_request_id,
                "- no active Inventory user with email.",
            )
            return False

        project_rows = list(
            ProjectInventory.objects
            .select_related("component")
            .filter(
                material_request=material_request
            )
            .order_by("id")
        )

        ready_rows = []
        total_ready_to_issue = 0
        total_qc_passed = 0
        total_qc_failed = 0

        for row in project_rows:
            remaining_required = max(
                int(
                    row.remaining_quantity
                    or 0
                ),
                0,
            )

            ready_store = max(
                int(
                    row.remaining_store_quantity
                    or 0
                ),
                0,
            )

            ready_purchased = max(
                int(
                    row.remaining_purchased_quantity
                    or 0
                ),
                0,
            )

            ready_now = min(
                remaining_required,
                ready_store
                + ready_purchased,
            )

            total_qc_passed += max(
                int(
                    row.qc_passed_quantity
                    or 0
                ),
                0,
            )

            total_qc_failed += max(
                int(
                    row.qc_failed_quantity
                    or 0
                ),
                0,
            )

            if ready_now <= 0:
                continue

            total_ready_to_issue += (
                ready_now
            )

            component = getattr(
                row,
                "component",
                None,
            )

            component_code = (
                getattr(
                    component,
                    "component_id",
                    "",
                )
                or getattr(
                    component,
                    "id",
                    "",
                )
                or ""
            )

            component_name = (
                getattr(
                    component,
                    "name",
                    "",
                )
                or "Component"
            )

            ready_rows.append(
                (
                    f"{component_code} - "
                    f"{component_name}: "
                    f"Store Ready {ready_store}, "
                    f"QC-Passed Purchased Ready "
                    f"{ready_purchased}, "
                    f"Ready to Issue {ready_now}"
                ).strip()
            )

        if total_ready_to_issue <= 0:
            print(
                "QC INVENTORY EMAIL SKIPPED:",
                material_request.material_request_id,
                "- no quantity is currently ready to issue.",
            )
            return False

        requester_name = (
            material_request.requester_name
            or self.get_user_display_name(
                material_request.requester,
                "Requester",
            )
        )

        if workflow_complete:
            subject = (
                f"{material_request.material_request_id} "
                f"QC completed - Inventory Action Required"
            )

            message = (
                f"QC is completed for "
                f"{material_request.material_request_id}. "
                f"QC-passed and reserved components are "
                f"ready for Inventory to provide."
            )

            status_label = (
                "QC Checked - Ready to Provide"
            )
        else:
            subject = (
                f"{material_request.material_request_id} "
                f"components ready after QC - "
                f"Inventory Action Required"
            )

            message = (
                f"New QC-passed components for "
                f"{material_request.material_request_id} "
                f"are ready for partial issue. "
                f"Other MR components may still be "
                f"awaiting delivery or QC."
            )

            status_label = (
                "Components Ready for Partial Issue"
            )

        component_summary = (
            "; ".join(ready_rows)
            if ready_rows
            else "-"
        )

        action_url = (
            f"{self.get_ipms_base_url()}"
            f"/notifications"
        )

        sent_any = False

        for inventory_user in inventory_users:
            sent = send_ipms_email(
                recipient_email=inventory_user.email,
                subject=subject,
                context={
                    "recipient_name": (
                        self.get_user_display_name(
                            inventory_user,
                            "Inventory",
                        )
                    ),
                    "message": message,
                    "table_headers": [
                        "MR ID",
                        "Project",
                        "Requested By",
                        "Ready to Issue",
                        "QC Passed",
                        "QC Failed",
                        "Components",
                        "Status",
                    ],
                    "table_values": [
                        material_request.material_request_id,
                        material_request.project,
                        requester_name,
                        total_ready_to_issue,
                        total_qc_passed,
                        total_qc_failed,
                        component_summary,
                        status_label,
                    ],
                    "status": status_label,
                    "instruction": (
                        "Please open the Inventory "
                        "notification and provide only "
                        "the quantities currently ready "
                        "for this Material Request."
                    ),
                    "button_text": (
                        "Provide Components in IPMS"
                    ),
                    "action_url": action_url,
                },
            )

            if sent:
                sent_any = True

        print(
            "QC INVENTORY EMAIL SENT =",
            sent_any,
            "| MR =",
            material_request.material_request_id,
            "| READY =",
            total_ready_to_issue,
            "| COMPLETE =",
            workflow_complete,
        )

        return sent_any

    def get_queryset(self):
        return (
            InwardEntry.objects
            .select_related(
                "vendor",
                "component",
                "purchase_order",
            )
            .prefetch_related("line_items")
            .filter(
                Q(removed_from_inventory=False)
                | Q(removed_from_inventory__isnull=True)
            )
            .order_by("-received_date", "-id")
        )

    @staticmethod
    def get_qc_row_quantity(row):
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

    @classmethod
    def get_qc_rows_quantity(cls, rows):
        return sum(
            cls.get_qc_row_quantity(row)
            for row in (rows or [])
            if isinstance(row, dict)
        )

    @staticmethod
    def get_source_mr_number(
        inward_entry,
    ):
        purchase_order = getattr(
            inward_entry,
            "purchase_order",
            None,
        )

        if not purchase_order:
            return ""

        return str(
            getattr(
                purchase_order,
                "source_mr_number",
                "",
            )
            or ""
        ).strip()

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

    @classmethod
    def get_qc_serials(cls, inward_entry, rows):
        serials = []
        seen = set()
        row_number = 0
        batch_value = str(inward_entry.code or inward_entry.id or "INWARD")
        digits = "".join(
            character for character in batch_value if character.isdigit()
        )[-5:].zfill(5)
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            quantity = max(cls.get_qc_row_quantity(row), 0)
            raw_serial = str(
                row.get("serialNumber")
                or row.get("serial_number")
                or row.get("serial")
                or ""
            ).strip()
            for offset in range(quantity):
                row_number += 1
                serial = raw_serial
                if quantity > 1 and raw_serial:
                    serial = f"{raw_serial}-{offset + 1}"
                if not serial:
                    serial = f"C_{digits}S{row_number:05d}"
                if serial not in seen:
                    seen.add(serial)
                    serials.append(serial)
        return serials

    @classmethod
    def sync_direct_inward_inventory(
        cls,
        inward_entry,
        *,
        passed_quantity,
        previous_passed_quantity,
        passed_rows=None,
    ):
        """
        Synchronize QC-passed Direct Inward stock into the central
        physical Inventory table.

        Purchase Orders linked to a Material Request are excluded because
        their QC-passed stock belongs to ProjectInventory, not free In Store.
        """
        source_mr_number = (
            cls.get_source_mr_number(
                inward_entry
            )
        )

        if source_mr_number:
            return None

        inventory_code = str(
            inward_entry.code or ""
        ).strip()

        if not inventory_code:
            raise serializers.ValidationError(
                {
                    "detail": (
                        "Direct Inward must have an "
                        "Inward code before QC stock "
                        "can be synchronized."
                    )
                }
            )

        passed_quantity = max(
            int(passed_quantity or 0),
            0,
        )

        previous_passed_quantity = max(
            int(previous_passed_quantity or 0),
            0,
        )

        stock_row = (
            Inventory.objects
            .select_for_update()
            .filter(
                inventory_code=inventory_code
            )
            .first()
        )

        passed_serials = cls.get_qc_serials(
            inward_entry,
            passed_rows or inward_entry.qc_passed_rows,
        )
        already_issued_quantity = 0
        issued_serials = []

        if stock_row is not None:
            issued_serials = cls.normalize_serials(
                stock_row.issued_serial_numbers
            )
            current_remaining_quantity = max(
                int(stock_row.quantity or 0),
                0,
            )

            already_issued_quantity = max(
                previous_passed_quantity
                - current_remaining_quantity,
                0,
            )

            if (
                stock_row.issued
                and current_remaining_quantity == 0
            ):
                already_issued_quantity = max(
                    already_issued_quantity,
                    previous_passed_quantity,
                )

        if passed_quantity < already_issued_quantity:
            raise serializers.ValidationError(
                {
                    "passedRows": [
                        (
                            "QC passed quantity cannot be "
                            "reduced below the quantity "
                            "already issued from In Store. "
                            f"Already issued: "
                            f"{already_issued_quantity}."
                        )
                    ]
                }
            )

        if issued_serials:
            missing_issued = [
                serial
                for serial in issued_serials
                if serial not in set(passed_serials)
            ]
            if missing_issued:
                raise serializers.ValidationError(
                    {
                        "passedRows": [
                            "QC serials already issued from In Store cannot be removed: "
                            + ", ".join(missing_issued)
                        ]
                    }
                )

        available_serials = [
            serial
            for serial in passed_serials
            if serial not in set(issued_serials)
        ]
        remaining_quantity = max(
            passed_quantity - already_issued_quantity,
            0,
        )
        if passed_serials:
            remaining_quantity = len(available_serials)

        vendor = getattr(
            inward_entry,
            "vendor",
            None,
        )

        vendor_name = (
            getattr(vendor, "name", "")
            or getattr(
                vendor,
                "vendor_name",
                "",
            )
            or str(vendor or "")
        )

        purchase_order = getattr(
            inward_entry,
            "purchase_order",
            None,
        )

        purchase_order_number = (
            getattr(
                purchase_order,
                "po_number",
                "",
            )
            or str(purchase_order or "")
        )

        total_price = (
            inward_entry.line_items.aggregate(
                total=Sum("grand_total")
            ).get("total")
            or 0
        )

        values = {
            "component":
                inward_entry.component,
            "category": (
                getattr(
                    inward_entry.component,
                    "category",
                    "",
                )
                or ""
            ),
            "vendor": vendor_name,
            "purchase_order":
                purchase_order_number,
            "quantity": remaining_quantity,
            "received_date":
                inward_entry.received_date,
            "total_price": total_price,
            "issued": (
                passed_quantity > 0
                and remaining_quantity == 0
            ),
            "serial_numbers": available_serials,
            "issued_serial_numbers": issued_serials,
        }

        if stock_row is None:
            if passed_quantity <= 0:
                return None

            return Inventory.objects.create(
                inventory_code=inventory_code,
                **values,
            )

        for field_name, value in values.items():
            setattr(
                stock_row,
                field_name,
                value,
            )

        stock_row.save(
            update_fields=[
                "component",
                "category",
                "vendor",
                "purchase_order",
                "quantity",
                "received_date",
                "total_price",
                "issued",
                "serial_numbers",
                "issued_serial_numbers",
            ]
        )

        return stock_row

    @staticmethod
    def get_material_request_items(
        material_request,
        *,
        lock=False,
    ):
        request_type = str(
            material_request.request_type or ""
        ).strip().upper()

        manager = (
            material_request.rd_items
            if request_type in {"R&D", "RD"}
            else material_request.bom_items
        )

        queryset = manager.all()

        if lock:
            queryset = queryset.select_for_update()

        return list(queryset)

    @staticmethod
    def distribute_quantity(items, total_quantity):
        """
        Distribute a component-level total across repeated MR rows.
        """
        remaining = max(int(total_quantity or 0), 0)
        result = {}

        for item in items:
            required = max(int(item.quantity or 0), 0)
            allocated = min(required, remaining)
            result[item.pk] = allocated
            remaining -= allocated

        return result

    @staticmethod
    def group_material_request_items(request_items):
        groups = defaultdict(
            lambda: {
                "items": [],
                "required_quantity": 0,
            }
        )

        for item in request_items:
            component_id = getattr(
                item,
                "component_id",
                None,
            )

            if not component_id:
                continue

            group = groups[int(component_id)]
            group["items"].append(item)
            group["required_quantity"] += max(
                int(item.quantity or 0),
                0,
            )

        return groups

    @staticmethod
    def upsert_inventory_notification(
        material_request,
        *,
        notification_status,
        title,
        message,
        is_read=False,
    ):
        reference_id = str(material_request.id)

        queryset = Notification.objects.filter(
            category="MR",
            receiver="INVENTORY",
            reference_id=reference_id,
        ).order_by("-id")

        notification = queryset.first()

        if notification is None:
            notification = Notification.objects.create(
                category="MR",
                receiver="INVENTORY",
                reference_id=reference_id,
                status=notification_status,
                title=title,
                message=message,
                is_read=is_read,
            )
        else:
            notification.status = notification_status
            notification.title = title
            notification.message = message
            notification.is_read = is_read
            notification.save(
                update_fields=[
                    "status",
                    "title",
                    "message",
                    "is_read",
                ]
            )

            queryset.exclude(
                pk=notification.pk
            ).delete()

        return notification

    @transaction.atomic
    def sync_material_request_qc_progress(
        self,
        source_mr_number,
    ):
        """
        Recalculate QC progress for every active PO and inward belonging
        to one Material Request.

        Existing In-Store reservations remain reserved. Only QC-passed
        Procurement quantity is added to ProjectInventory, and no
        quantity is marked issued in this method.
        """
        source_value = str(
            source_mr_number or ""
        ).strip()

        if not source_value:
            return None

        material_request = (
            MaterialRequest.objects
            .select_for_update()
            .filter(
                material_request_id=source_value
            )
            .first()
        )

        if not material_request:
            return None

        # ---------------------------------------------------------
        # Capture the state BEFORE this QC synchronization.
        #
        # This is used to avoid duplicate Inventory emails when the
        # same QC data is saved again. An email is sent when newly
        # QC-passed purchased quantity becomes available.
        # ---------------------------------------------------------
        previous_mr_status = str(
            material_request.status or ""
        ).strip().upper()

        previous_project_rows = list(
            ProjectInventory.objects
            .select_for_update()
            .filter(
                material_request=material_request
            )
        )

        previous_purchased_ready = sum(
            max(
                int(
                    row.remaining_purchased_quantity
                    or 0
                ),
                0,
            )
            for row in previous_project_rows
        )

        canonical_mr_number = str(
            material_request.material_request_id
        ).strip()

        related_purchase_orders = list(
            PurchaseOrder.objects
            .select_for_update()
            .filter(
                source_mr_number=canonical_mr_number
            )
            .exclude(
                status__in=[
                    "REJECTED",
                    "FINANCE_REJECTED",
                ]
            )
        )

        related_po_ids = [
            purchase_order.id
            for purchase_order
            in related_purchase_orders
        ]

        related_inwards = list(
            InwardEntry.objects
            .select_for_update()
            .select_related(
                "component",
                "purchase_order",
            )
            .filter(
                purchase_order_id__in=related_po_ids,
            )
            .filter(
                Q(removed_from_inventory=False)
                | Q(
                    removed_from_inventory__isnull=True
                )
            )
        )

        request_items = self.get_material_request_items(
            material_request,
            lock=True,
        )

        groups = self.group_material_request_items(
            request_items
        )

        active_po_exists = bool(
            related_purchase_orders
        )

        all_purchase_orders_delivered = (
            active_po_exists
            and all(
                str(
                    purchase_order.status or ""
                ).strip().upper()
                == "DELIVERED"
                for purchase_order
                in related_purchase_orders
            )
        )

        inward_exists = bool(related_inwards)

        all_inwards_qc_completed = (
            inward_exists
            and all(
                str(
                    inward.qc_status or ""
                ).strip().upper()
                in {
                    "COMPLETED",
                    "PASS",
                    "FAIL",
                }
                for inward in related_inwards
            )
        )

        all_inward_quantities_inspected = (
            inward_exists
            and all(
                (
                    self.get_qc_rows_quantity(
                        inward.qc_passed_rows
                    )
                    + self.get_qc_rows_quantity(
                        inward.qc_failed_rows
                    )
                )
                == int(
                    inward.quantity_received or 0
                )
                for inward in related_inwards
            )
        )

        component_summaries = []

        # Refresh component-level QC totals even before the complete MR
        # reaches QC_CHECKED.
        for component_id, group in groups.items():
            items = group["items"]
            required_quantity = int(
                group["required_quantity"] or 0
            )

            component_inwards = [
                inward
                for inward in related_inwards
                if str(inward.component_id)
                == str(component_id)
            ]

            passed_quantity = sum(
                self.get_qc_rows_quantity(
                    inward.qc_passed_rows
                )
                for inward in component_inwards
            )

            failed_quantity = sum(
                self.get_qc_rows_quantity(
                    inward.qc_failed_rows
                )
                for inward in component_inwards
            )

            purchased_serial_numbers = []
            purchased_serial_seen = set()
            for inward in sorted(
                component_inwards,
                key=lambda row: (row.received_date, row.id),
            ):
                for serial in self.get_qc_serials(
                    inward,
                    inward.qc_passed_rows,
                ):
                    if serial not in purchased_serial_seen:
                        purchased_serial_seen.add(serial)
                        purchased_serial_numbers.append(serial)

            passed_distribution = (
                self.distribute_quantity(
                    items,
                    passed_quantity,
                )
            )
            failed_distribution = (
                self.distribute_quantity(
                    items,
                    failed_quantity,
                )
            )

            for request_item in items:
                changed_fields = []

                item_passed = passed_distribution.get(
                    request_item.pk,
                    0,
                )
                item_failed = failed_distribution.get(
                    request_item.pk,
                    0,
                )

                if (
                    int(
                        request_item.qc_passed_quantity
                        or 0
                    )
                    != item_passed
                ):
                    request_item.qc_passed_quantity = (
                        item_passed
                    )
                    changed_fields.append(
                        "qc_passed_quantity"
                    )

                if (
                    int(
                        request_item.qc_failed_quantity
                        or 0
                    )
                    != item_failed
                ):
                    request_item.qc_failed_quantity = (
                        item_failed
                    )
                    changed_fields.append(
                        "qc_failed_quantity"
                    )

                if changed_fields:
                    request_item.save(
                        update_fields=changed_fields
                    )

            reservation = (
                InventoryReservation.objects
                .select_for_update()
                .filter(
                    material_request=material_request,
                    component_id=component_id,
                )
                .first()
            )

            reserved_store_quantity = min(
                required_quantity,
                int(
                    reservation.reserved_store_quantity
                    if reservation
                    else 0
                ),
            )

            procurement_requirement = max(
                required_quantity
                - reserved_store_quantity,
                0,
            )

            purchased_ready_quantity = min(
                passed_quantity,
                procurement_requirement,
            )

            project_row, _ = (
                ProjectInventory.objects
                .select_for_update()
                .get_or_create(
                    material_request=material_request,
                    component_id=component_id,
                    defaults={
                        "project": (
                            material_request.project
                        ),
                        "requested_quantity": (
                            required_quantity
                        ),
                    },
                )
            )

            project_row.project = (
                material_request.project
            )
            project_row.requested_quantity = (
                required_quantity
            )
            project_row.store_quantity = (
                reserved_store_quantity
            )
            project_row.purchased_quantity = (
                purchased_ready_quantity
            )
            project_row.qc_passed_quantity = (
                passed_quantity
            )
            project_row.qc_failed_quantity = (
                failed_quantity
            )
            project_row.quantity = min(
                required_quantity,
                reserved_store_quantity
                + purchased_ready_quantity,
            )
            project_row.purchased_serial_numbers = (
                purchased_serial_numbers
            )
            project_row.save()

            component_summaries.append(
                {
                    "component_id": component_id,
                    "required_quantity": (
                        required_quantity
                    ),
                    "reserved_store_quantity": (
                        reserved_store_quantity
                    ),
                    "purchased_ready_quantity": (
                        purchased_ready_quantity
                    ),
                    "qc_passed_quantity": (
                        passed_quantity
                    ),
                    "qc_failed_quantity": (
                        failed_quantity
                    ),
                }
            )

        workflow_qc_complete = (
            all_purchase_orders_delivered
            and all_inwards_qc_completed
            and all_inward_quantities_inspected
        )

        # Re-read the synchronized ProjectInventory rows. They are the
        # authoritative source for what Inventory can provide right now.
        current_project_rows = list(
            ProjectInventory.objects
            .select_for_update()
            .filter(
                material_request=material_request
            )
        )

        total_ready_to_issue = sum(
            min(
                max(
                    int(row.remaining_quantity or 0),
                    0,
                ),
                max(
                    int(
                        row.remaining_store_quantity
                        or 0
                    ),
                    0,
                )
                + max(
                    int(
                        row.remaining_purchased_quantity
                        or 0
                    ),
                    0,
                ),
            )
            for row in current_project_rows
        )

        ready_component_count = sum(
            1
            for row in current_project_rows
            if (
                int(
                    row.remaining_store_quantity
                    or 0
                )
                + int(
                    row.remaining_purchased_quantity
                    or 0
                )
            ) > 0
            and int(row.remaining_quantity or 0) > 0
        )

        current_purchased_ready = sum(
            max(
                int(
                    row.remaining_purchased_quantity
                    or 0
                ),
                0,
            )
            for row in current_project_rows
        )

        newly_qc_ready_quantity = max(
            current_purchased_ready
            - previous_purchased_ready,
            0,
        )

        any_delivery_progress = (
            any(
                str(
                    purchase_order.status or ""
                ).strip().upper()
                in {
                    "PARTIALLY_DELIVERED",
                    "DELIVERED",
                }
                for purchase_order
                in related_purchase_orders
            )
            or any(
                int(
                    inward.quantity_received
                    or 0
                ) > 0
                for inward in related_inwards
            )
        )

        if not workflow_qc_complete:
            current_status = str(
                material_request.status or ""
            ).strip().upper()

            # Keep the MR's OVERALL status truthful while component-level
            # Inventory fulfillment proceeds independently.
            if current_status not in {
                "INVENTORY_ISSUED",
                "MR_COMPLETED",
            }:
                desired_status = current_status

                if all_purchase_orders_delivered:
                    desired_status = "PO_DELIVERED"
                elif any_delivery_progress:
                    desired_status = (
                        "PARTIALLY_DELIVERED"
                    )

                if (
                    desired_status
                    and desired_status
                    != current_status
                ):
                    material_request.status = (
                        desired_status
                    )
                    material_request.po_raised = True
                    material_request.save(
                        update_fields=[
                            "status",
                            "po_raised",
                        ]
                    )

            # CRITICAL BUSINESS RULE:
            # one QC-passed component must be issuable immediately even
            # while other BOM components are still awaiting delivery/QC.
            if total_ready_to_issue > 0:
                self.upsert_inventory_notification(
                    material_request,
                    notification_status=(
                        "PROJECT_INVENTORY_READY"
                    ),
                    title=(
                        "Components Ready for Partial Issue - "
                        f"{material_request.material_request_id}"
                    ),
                    message=(
                        f"{ready_component_count} component "
                        f"type(s), totaling "
                        f"{total_ready_to_issue} unit(s), "
                        "are currently ready to provide to "
                        "the engineer. Other MR components "
                        "may still be awaiting delivery or "
                        "QC. Open the MR and provide only "
                        "the quantities that are ready."
                    ),
                    is_read=False,
                )

                # Send an email only when THIS QC update added new
                # QC-passed purchased quantity. Re-saving identical
                # QC data will not resend the same email.
                if newly_qc_ready_quantity > 0:
                    transaction.on_commit(
                        lambda mr_id=material_request.id: (
                            self.send_inventory_qc_ready_email(
                                mr_id,
                                workflow_complete=False,
                            )
                        )
                    )

            return material_request

        current_status = str(
            material_request.status or ""
        ).strip().upper()

        # Never downgrade a completed Inventory workflow.
        if current_status in {
            "INVENTORY_ISSUED",
            "MR_COMPLETED",
        }:
            return material_request

        material_request.status = "QC_CHECKED"
        material_request.po_raised = True
        material_request.save(
            update_fields=[
                "status",
                "po_raised",
            ]
        )

        total_passed_quantity = sum(
            row["qc_passed_quantity"]
            for row in component_summaries
        )
        total_failed_quantity = sum(
            row["qc_failed_quantity"]
            for row in component_summaries
        )
        total_reserved_store = sum(
            row["reserved_store_quantity"]
            for row in component_summaries
        )
        total_purchased_ready = sum(
            row["purchased_ready_quantity"]
            for row in component_summaries
        )

        self.upsert_inventory_notification(
            material_request,
            notification_status="QC_CHECKED",
            title=(
                "QC Passed Components - "
                f"{material_request.material_request_id}"
            ),
            message=(
                f"QC is completed for "
                f"{material_request.material_request_id}. "
                f"Reserved from In Store: "
                f"{total_reserved_store}. "
                f"QC-passed Procurement quantity ready: "
                f"{total_purchased_ready}. "
                f"Total QC passed: "
                f"{total_passed_quantity}. "
                f"Total QC failed: "
                f"{total_failed_quantity}. "
                "Provide the QC-passed and reserved In-Store "
                "quantities source-wise."
            ),
            is_read=False,
        )

        # Final QC-ready email:
        # - send when QC has newly made purchased stock available, OR
        # - send once when the MR first enters the complete QC stage
        #   and there is something for Inventory to provide.
        should_send_complete_qc_email = (
            total_ready_to_issue > 0
            and (
                newly_qc_ready_quantity > 0
                or previous_mr_status
                != "QC_CHECKED"
            )
        )

        if should_send_complete_qc_email:
            transaction.on_commit(
                lambda mr_id=material_request.id: (
                    self.send_inventory_qc_ready_email(
                        mr_id,
                        workflow_complete=True,
                    )
                )
            )

        return material_request

    @action(
        detail=False,
        methods=["get"],
        url_path="next-code",
    )
    def next_code(self, request):
        last = (
            InwardEntry.objects
            .order_by("-id")
            .first()
        )

        if last and last.code:
            try:
                import re

                match = re.search(
                    r"\d+",
                    last.code,
                )
                last_no = (
                    int(match.group())
                    if match
                    else 0
                )
            except ValueError:
                last_no = 0
        else:
            last_no = 0

        next_code = (
            f"INW-{last_no + 1:03d}"
        )

        return Response(
            {
                "inward_code": next_code
            }
        )

    @staticmethod
    def recalculate_purchase_order_receipts(
        purchase_order,
    ):
        """
        Rebuild PurchaseOrderItem.received_quantity from the remaining
        InwardEntry rows after an Inward deletion.

        This avoids leaving a Purchase Order marked Delivered after its
        corresponding Inward record has been removed.
        """
        purchase_order_items = list(
            PurchaseOrderItem.objects
            .select_for_update()
            .filter(
                purchase_order=purchase_order
            )
            .order_by("id")
        )

        remaining_inwards = list(
            InwardEntry.objects
            .select_for_update()
            .filter(
                purchase_order=purchase_order
            )
            .filter(
                Q(removed_from_inventory=False)
                | Q(
                    removed_from_inventory__isnull=True
                )
            )
            .order_by("received_date", "id")
        )

        received_by_component = defaultdict(int)

        for inward in remaining_inwards:
            if inward.component_id is None:
                continue

            received_by_component[
                int(inward.component_id)
            ] += max(
                int(inward.quantity_received or 0),
                0,
            )

        items_by_component = defaultdict(list)

        for po_item in purchase_order_items:
            if po_item.component_id is None:
                continue

            items_by_component[
                int(po_item.component_id)
            ].append(po_item)

        for component_id, component_items in (
            items_by_component.items()
        ):
            unallocated_received = int(
                received_by_component.get(
                    component_id,
                    0,
                )
            )

            for po_item in component_items:
                ordered_quantity = max(
                    int(po_item.quantity or 0),
                    0,
                )

                allocated_quantity = min(
                    ordered_quantity,
                    unallocated_received,
                )

                if (
                    int(
                        po_item.received_quantity
                        or 0
                    )
                    != allocated_quantity
                ):
                    po_item.received_quantity = (
                        allocated_quantity
                    )
                    po_item.save(
                        update_fields=[
                            "received_quantity",
                        ]
                    )

                unallocated_received = max(
                    unallocated_received
                    - allocated_quantity,
                    0,
                )

        total_ordered = sum(
            max(int(item.quantity or 0), 0)
            for item in purchase_order_items
        )

        total_received = sum(
            max(
                int(
                    item.received_quantity
                    or 0
                ),
                0,
            )
            for item in purchase_order_items
        )

        all_delivered = (
            bool(purchase_order_items)
            and all(
                int(
                    item.received_quantity
                    or 0
                )
                >= int(item.quantity or 0)
                for item in purchase_order_items
            )
        )

        if all_delivered:
            new_status = "DELIVERED"
        elif total_received > 0:
            new_status = "PARTIALLY_DELIVERED"
        else:
            new_status = "ORDERED"

        if (
            str(
                purchase_order.status or ""
            ).strip().upper()
            != new_status
        ):
            purchase_order.status = new_status
            purchase_order.save(
                update_fields=["status"]
            )

        return {
            "ordered_quantity": total_ordered,
            "received_quantity": total_received,
            "status": new_status,
        }

    def downgrade_material_request_after_delete(
        self,
        source_mr_number,
    ):
        """
        Recalculate QC/Project Inventory quantities and move an MR back
        to the correct PO stage when the deleted Inward made the MR
        incomplete.
        """
        source_value = str(
            source_mr_number or ""
        ).strip()

        if not source_value:
            return None

        # This updates component QC totals and ProjectInventory rows.
        material_request = (
            self.sync_material_request_qc_progress(
                source_value
            )
        )

        if not material_request:
            return None

        current_status = str(
            material_request.status or ""
        ).strip().upper()

        if current_status in {
            "INVENTORY_ISSUED",
            "MR_COMPLETED",
        }:
            return material_request

        active_purchase_orders = list(
            PurchaseOrder.objects
            .select_for_update()
            .filter(
                source_mr_number=source_value
            )
            .exclude(
                status__in=[
                    "REJECTED",
                    "FINANCE_REJECTED",
                ]
            )
        )

        all_purchase_orders_delivered = (
            bool(active_purchase_orders)
            and all(
                str(
                    purchase_order.status or ""
                ).strip().upper()
                == "DELIVERED"
                for purchase_order
                in active_purchase_orders
            )
        )

        related_po_ids = [
            purchase_order.id
            for purchase_order
            in active_purchase_orders
        ]

        related_inwards = list(
            InwardEntry.objects
            .select_for_update()
            .filter(
                purchase_order_id__in=related_po_ids
            )
            .filter(
                Q(removed_from_inventory=False)
                | Q(
                    removed_from_inventory__isnull=True
                )
            )
        )

        all_qc_complete = (
            bool(related_inwards)
            and all(
                str(
                    inward.qc_status or ""
                ).strip().upper()
                in {
                    "COMPLETED",
                    "PASS",
                    "FAIL",
                }
                and (
                    self.get_qc_rows_quantity(
                        inward.qc_passed_rows
                    )
                    +
                    self.get_qc_rows_quantity(
                        inward.qc_failed_rows
                    )
                )
                == int(
                    inward.quantity_received or 0
                )
                for inward in related_inwards
            )
        )

        if (
            all_purchase_orders_delivered
            and all_qc_complete
        ):
            # sync_material_request_qc_progress already set QC_CHECKED.
            return material_request

        material_request.status = (
            "PO_DELIVERED"
            if all_purchase_orders_delivered
            else "PO_RAISED"
        )
        material_request.po_raised = True
        material_request.save(
            update_fields=[
                "status",
                "po_raised",
            ]
        )

        # The QC-passed Inventory action is no longer valid.
        Notification.objects.filter(
            category="MR",
            receiver="INVENTORY",
            reference_id=str(
                material_request.id
            ),
        ).delete()

        return material_request

    @transaction.atomic
    def destroy(
        self,
        request,
        *args,
        **kwargs,
    ):
        """
        Permanently delete an Inward entry and its line items.

        Deletion is blocked once any quantity from this MR component has
        been issued, because removing its source document after issue
        would corrupt inventory history.
        """
        try:
            instance = (
                InwardEntry.objects
                .select_for_update()
                .select_related(
                    "purchase_order",
                    "component",
                )
                .get(pk=kwargs.get("pk"))
            )
        except InwardEntry.DoesNotExist:
            return Response(
                {
                    "detail":
                        "Inward entry was not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        purchase_order = instance.purchase_order
        component_id = instance.component_id

        source_mr_number = ""

        if purchase_order:
            source_mr_number = str(
                purchase_order.source_mr_number
                or ""
            ).strip()

        material_request = None

        direct_inventory_row = None

        if not source_mr_number:
            direct_inventory_row = (
                Inventory.objects
                .select_for_update()
                .filter(
                    inventory_code=instance.code
                )
                .first()
            )

            if direct_inventory_row:
                original_passed_quantity = (
                    self.get_qc_rows_quantity(
                        instance.qc_passed_rows
                    )
                )

                remaining_quantity = max(
                    int(
                        direct_inventory_row.quantity
                        or 0
                    ),
                    0,
                )

                issued_quantity = max(
                    int(
                        original_passed_quantity
                        or 0
                    )
                    - remaining_quantity,
                    0,
                )

                if (
                    direct_inventory_row.issued
                    and remaining_quantity == 0
                ):
                    issued_quantity = max(
                        issued_quantity,
                        int(
                            original_passed_quantity
                            or 0
                        ),
                    )

                if issued_quantity > 0:
                    return Response(
                        {
                            "detail": (
                                "This Direct Inward "
                                "stock has already been "
                                "issued from In Store. "
                                "Reverse the issue before "
                                "deleting the Inward entry."
                            )
                        },
                        status=
                            status.HTTP_409_CONFLICT,
                    )

        if source_mr_number:
            material_request = (
                MaterialRequest.objects
                .select_for_update()
                .filter(
                    material_request_id=(
                        source_mr_number
                    )
                )
                .first()
            )

        if material_request and component_id:
            project_row = (
                ProjectInventory.objects
                .select_for_update()
                .filter(
                    material_request=(
                        material_request
                    ),
                    component_id=component_id,
                )
                .first()
            )

            if (
                project_row
                and int(
                    project_row.issued_quantity
                    or 0
                ) > 0
            ):
                return Response(
                    {
                        "detail": (
                            "This QC-passed component "
                            "has already been issued. "
                            "Reverse the issue before "
                            "deleting its Inward entry."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        if material_request:
            mr_status = str(
                material_request.status or ""
            ).strip().upper()

            if mr_status in {
                "INVENTORY_ISSUED",
                "MR_COMPLETED",
            }:
                return Response(
                    {
                        "detail": (
                            "This Material Request is "
                            "already completed or issued. "
                            "Its Inward entry cannot be "
                            "deleted."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        deleted_id = instance.id

        if direct_inventory_row is not None:
            direct_inventory_row.delete()

        # Hard delete. Related InwardLineItem rows are removed through
        # their ForeignKey cascade.
        instance.delete()

        po_progress = None

        if purchase_order:
            locked_purchase_order = (
                PurchaseOrder.objects
                .select_for_update()
                .get(pk=purchase_order.pk)
            )

            po_progress = (
                self
                .recalculate_purchase_order_receipts(
                    locked_purchase_order
                )
            )

        if source_mr_number:
            self.downgrade_material_request_after_delete(
                source_mr_number
            )

        return Response(
            {
                "detail": (
                    "Inward entry and linked line "
                    "items were deleted permanently."
                ),
                "deleted_id": deleted_id,
                "purchase_order": po_progress,
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="qc",
    )
    @transaction.atomic
    def qc(self, request, pk=None):
        """
        Save QC results for one inward entry.

        Important:
        - Pass and Fail remarks are mandatory.
        - Partial QC is allowed: inspected quantity may be less than received.
        - MR-linked PO stock is synchronized to ProjectInventory immediately.
        - Direct Inward QC-passed stock is synchronized to the central
          physical In-Store Inventory table.
        - Failed quantity is never added to Inventory.
        """

        try:
            inward_entry = (
                InwardEntry.objects
                .select_for_update()
                .select_related(
                    "component",
                    "vendor",
                    "purchase_order",
                )
                .prefetch_related(
                    "line_items"
                )
                .get(pk=pk)
            )
        except InwardEntry.DoesNotExist:
            return Response(
                {
                    "detail":
                        "Inward entry not found."
                },
                status=
                    status.HTTP_404_NOT_FOUND,
            )

        serializer = InwardQCSerializer(
            data=request.data
        )
        serializer.is_valid(
            raise_exception=True
        )

        passed_rows = (
            serializer.validated_data.get(
                "passedRows",
                [],
            )
        )

        failed_rows = (
            serializer.validated_data.get(
                "failedRows",
                [],
            )
        )

        passed_quantity = (
            self.get_qc_rows_quantity(
                passed_rows
            )
        )

        failed_quantity = (
            self.get_qc_rows_quantity(
                failed_rows
            )
        )

        inspected_quantity = (
            passed_quantity
            + failed_quantity
        )

        received_quantity = int(
            inward_entry.quantity_received
            or 0
        )

        if inspected_quantity <= 0:
            return Response(
                {
                    "detail": (
                        "Inspect at least one component before "
                        "submitting QC progress."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if inspected_quantity > received_quantity:
            return Response(
                {
                    "detail": (
                        "QC Pass quantity plus QC Fail quantity "
                        "cannot exceed the received quantity of "
                        f"{received_quantity}. Currently inspected: "
                        f"{inspected_quantity}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        previous_passed_quantity = (
            self.get_qc_rows_quantity(
                inward_entry.qc_passed_rows
            )
        )

        inward_entry.qc_passed_rows = (
            passed_rows
        )
        inward_entry.qc_failed_rows = (
            failed_rows
        )
        inward_entry.qc_status = (
            "COMPLETED"
            if inspected_quantity == received_quantity
            else "PARTIALLY_INSPECTED"
        )
        inward_entry.qc_timestamp = (
            serializer.validated_data.get(
                "timestamp"
            )
            or timezone.now()
        )

        top_level_remarks = str(
            serializer.validated_data.get(
                "remarks"
            )
            or ""
        ).strip()

        if top_level_remarks:
            inward_entry.remarks = (
                top_level_remarks
            )

        update_fields = [
            "qc_passed_rows",
            "qc_failed_rows",
            "qc_status",
            "qc_timestamp",
            "updated_at",
        ]

        if top_level_remarks:
            update_fields.append("remarks")

        inward_entry.save(
            update_fields=update_fields
        )

        source_mr_number = (
            self.get_source_mr_number(
                inward_entry
            )
        )

        material_request = None
        inventory_row = None

        if source_mr_number:
            material_request = (
                self
                .sync_material_request_qc_progress(
                    source_mr_number
                )
            )
        else:
            inventory_row = (
                self
                .sync_direct_inward_inventory(
                    inward_entry,
                    passed_quantity=(
                        passed_quantity
                    ),
                    previous_passed_quantity=(
                        previous_passed_quantity
                    ),
                    passed_rows=passed_rows,
                )
            )

        return Response(
            {
                "id": inward_entry.id,
                "qc_status":
                    inward_entry.qc_status,
                "passedRows":
                    inward_entry.qc_passed_rows,
                "failedRows":
                    inward_entry.qc_failed_rows,
                "passedCount":
                    passed_quantity,
                "failedCount":
                    failed_quantity,
                "inspectedCount":
                    inspected_quantity,
                "remainingCount":
                    max(
                        received_quantity - inspected_quantity,
                        0,
                    ),
                "timestamp":
                    inward_entry.qc_timestamp,
                "source_mr_number":
                    source_mr_number,
                "inventory_code": (
                    inventory_row.inventory_code
                    if inventory_row
                    else None
                ),
                "in_store_quantity": (
                    int(
                        inventory_row.quantity
                        or 0
                    )
                    if inventory_row
                    else None
                ),
                "mr_status": (
                    material_request.status
                    if material_request
                    else None
                ),
            },
            status=status.HTTP_200_OK,
        )