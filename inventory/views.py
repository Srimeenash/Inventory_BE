from collections import defaultdict

from django.db import transaction
from django.conf import settings
from django.utils import timezone
from django.db.models import Q

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inward.models import InwardEntry
from materialrequest.models import MaterialRequest
from notifications.models import Notification
from notifications.email_service import send_ipms_email
from procurement.models import PurchaseOrder

from .models import (
    Inventory,
    InventoryReservation,
    ProjectInventory,
)
from .serializers import (
    InventorySerializer,
    ProjectInventorySerializer,
)


class InventoryViewSet(viewsets.ModelViewSet):
    """
    Physical In-Store inventory API.
    """

    queryset = (
        Inventory.objects
        .select_related("component")
        .all()
        .order_by("-created_at")
    )

    serializer_class = InventorySerializer
    pagination_class = None

    @action(
        detail=False,
        methods=["get"],
        url_path="next-code",
    )
    def next_code(self, request):
        last = Inventory.objects.order_by("-id").first()

        last_no = 0

        if last and last.inventory_code:
            raw_code = str(last.inventory_code).strip()

            for prefix in ("INV-", "INV"):
                raw_code = raw_code.replace(prefix, "")

            try:
                last_no = int(raw_code)
            except (TypeError, ValueError):
                last_no = 0

        return Response(
            {
                "inventory_code": (
                    f"INV{last_no + 1:05d}"
                )
            }
        )


class ProjectInventoryViewSet(
    viewsets.ReadOnlyModelViewSet
):
    """
    Project Inventory API.

    The sync-mr action performs two operations:

    1. Synchronizes reserved Store and QC-passed purchased quantities.
    2. Provides selected quantities from STORE, PURCHASED, or both.

    Existing In-Store reservation is physically deducted only when the
    STORE source is provided.
    """

    queryset = (
        ProjectInventory.objects
        .select_related(
            "material_request",
            "component",
        )
        .all()
        .order_by("-updated_at")
    )

    serializer_class = ProjectInventorySerializer
    pagination_class = None


    # ==========================================================
    # MR REQUESTER - ALL COMPONENTS ISSUED EMAIL
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

    def send_requester_all_components_issued_email(
        self,
        material_request_id,
        *,
        issued_by_name="Inventory Team",
    ):
        """
        Send the final Inventory completion email to the original
        MR requester only after every ProjectInventory row is fulfilled.

        The email summarizes ALL issued quantities for the MR, not only
        the quantities from the last Provide Components action.
        """
        try:
            material_request = (
                MaterialRequest.objects
                .select_related("requester")
                .get(pk=material_request_id)
            )
        except MaterialRequest.DoesNotExist:
            return False

        requester = getattr(
            material_request,
            "requester",
            None,
        )

        requester_email = str(
            getattr(
                requester,
                "email",
                "",
            )
            or ""
        ).strip()

        if not requester_email:
            return False

        project_rows = list(
            ProjectInventory.objects
            .select_related("component")
            .filter(
                material_request=material_request
            )
            .order_by("id")
        )

        if not project_rows:
            return False

        if not all(
            row.is_fulfilled
            for row in project_rows
        ):
            return False

        requester_name = (
            material_request.requester_name
            or self.get_user_display_name(
                requester,
                "Requester",
            )
        )

        total_requested = sum(
            max(
                int(
                    row.requested_quantity
                    or 0
                ),
                0,
            )
            for row in project_rows
        )

        total_issued = sum(
            max(
                int(
                    row.issued_quantity
                    or 0
                ),
                0,
            )
            for row in project_rows
        )

        component_lines = []

        for row in project_rows:
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

            serials = self.normalize_serials(
                list(
                    row.issued_store_serials
                    or []
                )
                + list(
                    row.issued_purchased_serials
                    or []
                )
            )

            serial_text = (
                ", ".join(serials)
                if serials
                else "-"
            )

            component_lines.append(
                (
                    f"{component_code} - "
                    f"{component_name}: "
                    f"Requested {int(row.requested_quantity or 0)}, "
                    f"Issued {int(row.issued_quantity or 0)}, "
                    f"Serial(s): {serial_text}"
                ).strip()
            )

        component_summary = "; ".join(
            component_lines
        )

        subject = (
            f"Components issued for "
            f"{material_request.material_request_id} "
            f"- Inventory Update"
        )

        return send_ipms_email(
            recipient_email=requester_email,
            subject=subject,
            context={
                "recipient_name": requester_name,
                "message": (
                    f"All requested components for "
                    f"{material_request.material_request_id} "
                    f"have been issued by Inventory."
                ),
                "table_headers": [
                    "MR ID",
                    "Project",
                    "Requested By",
                    "Issued By",
                    "Issued Date",
                    "Requested Qty",
                    "Issued Qty",
                    "Components / Serials",
                    "Status",
                ],
                "table_values": [
                    material_request.material_request_id,
                    material_request.project,
                    requester_name,
                    issued_by_name
                    or "Inventory Team",
                    timezone.localdate().strftime(
                        "%d/%m/%Y"
                    ),
                    total_requested,
                    total_issued,
                    component_summary,
                    "All Components Issued",
                ],
                "status": (
                    "All Components Issued"
                ),
                "instruction": (
                    "All requested quantities are now "
                    "issued. Please review the completed "
                    "Material Request in IPMS."
                ),
                "button_text": (
                    "View Material Request in IPMS"
                ),
                "action_url": (
                    f"{self.get_ipms_base_url()}"
                    f"/material-requests"
                ),
            },
        )


    def get_queryset(self):
        queryset = super().get_queryset()

        material_request = str(
            self.request.query_params.get(
                "material_request",
                "",
            )
        ).strip()

        source_mr_number = str(
            self.request.query_params.get(
                "source_mr_number",
                "",
            )
        ).strip()

        project = str(
            self.request.query_params.get(
                "project",
                "",
            )
        ).strip()

        component = str(
            self.request.query_params.get(
                "component",
                "",
            )
        ).strip()

        if material_request:
            if material_request.isdigit():
                queryset = queryset.filter(
                    material_request_id=int(
                        material_request
                    )
                )
            else:
                queryset = queryset.filter(
                    material_request__material_request_id=(
                        material_request
                    )
                )

        if source_mr_number:
            queryset = queryset.filter(
                material_request__material_request_id=(
                    source_mr_number
                )
            )

        if project:
            queryset = queryset.filter(
                project__icontains=project
            )

        if component:
            queryset = queryset.filter(
                Q(component_id=component)
                | Q(
                    component__component_id=component
                )
                | Q(
                    component__name__icontains=component
                )
            )

        return queryset

    @staticmethod
    def resolve_material_request(
        reference,
        *,
        lock=False,
    ):
        reference_value = str(
            reference or ""
        ).strip()

        if not reference_value:
            return None

        queryset = MaterialRequest.objects

        if lock:
            queryset = queryset.select_for_update()

        lookup = Q(
            material_request_id=reference_value
        )

        if reference_value.isdigit():
            lookup |= Q(pk=int(reference_value))

        return queryset.filter(lookup).first()

    @staticmethod
    def get_request_items(
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

        queryset = manager.all().order_by("id")

        if lock:
            queryset = queryset.select_for_update()

        return list(queryset)

    @classmethod
    def get_component_groups(
        cls,
        material_request,
        *,
        lock=False,
    ):
        groups = defaultdict(
            lambda: {
                "items": [],
                "required_quantity": 0,
            }
        )

        for item in cls.get_request_items(
            material_request,
            lock=lock,
        ):
            component_id = getattr(
                item,
                "component_id",
                None,
            )

            if not component_id:
                continue

            group = groups[component_id]
            group["items"].append(item)
            group["required_quantity"] += max(
                int(item.quantity or 0),
                0,
            )

        return groups

    @staticmethod
    def distribute_quantity(
        items,
        total_quantity,
    ):
        remaining = max(int(total_quantity or 0), 0)
        result = {}

        for item in items:
            required = max(int(item.quantity or 0), 0)
            allocated = min(required, remaining)
            result[item.pk] = allocated
            remaining -= allocated

        return result

    @staticmethod
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

    @classmethod
    def get_qc_rows_quantity(cls, rows):
        return sum(
            cls.get_qc_row_quantity(row)
            for row in (rows or [])
        )

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
    def get_inward_passed_serials(cls, inward_entry):
        serials = []
        seen = set()
        row_number = 0
        batch_value = str(
            inward_entry.code
            or inward_entry.id
            or "INWARD"
        )
        digits = "".join(
            character for character in batch_value if character.isdigit()
        )[-5:].zfill(5)

        for row in inward_entry.qc_passed_rows or []:
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
    def ensure_stock_serials(cls, stock_row):
        serials = cls.normalize_serials(stock_row.serial_numbers)
        quantity = max(int(stock_row.quantity or 0), 0)
        prefix = "".join(
            character
            for character in str(stock_row.inventory_code or f"INV{stock_row.pk}")
            if character.isalnum()
        ).upper() or f"INV{stock_row.pk}"
        seen = set(serials) | set(
            cls.normalize_serials(stock_row.issued_serial_numbers)
        )
        index = 1
        while len(serials) < quantity:
            serial = f"CINV_{prefix}_S{index:05d}"
            index += 1
            if serial in seen:
                continue
            seen.add(serial)
            serials.append(serial)
        if serials != cls.normalize_serials(stock_row.serial_numbers):
            stock_row.serial_numbers = serials[:quantity]
            stock_row.save(update_fields=["serial_numbers"])
        return serials[:quantity]

    @classmethod
    def deduct_store_fifo(
        cls,
        component_id,
        requested_quantity,
        selected_serials=None,
    ):
        """Deduct physical Store stock and return quantity + exact serials."""
        requested = max(int(requested_quantity or 0), 0)
        selected = cls.normalize_serials(selected_serials)
        if selected and len(selected) != requested:
            raise ValueError(
                "Selected In-Store serial count must equal the issue quantity."
            )
        if requested == 0:
            return 0, []

        stock_rows = list(
            Inventory.objects.select_for_update().filter(
                component_id=component_id,
                issued=False,
                quantity__gt=0,
            ).order_by("received_date", "id")
        )

        available_by_serial = {}
        available_in_order = []
        for stock_row in stock_rows:
            for serial in cls.ensure_stock_serials(stock_row):
                if serial not in available_by_serial:
                    available_by_serial[serial] = stock_row
                    available_in_order.append(serial)

        chosen = selected or available_in_order[:requested]
        missing = [serial for serial in chosen if serial not in available_by_serial]
        if missing:
            raise ValueError(
                "One or more selected In-Store serials are no longer available: "
                + ", ".join(missing)
            )
        if len(chosen) < requested:
            raise ValueError(
                f"Only {len(chosen)} serial-tracked In-Store item(s) are available; "
                f"{requested} were requested."
            )

        remaining_chosen = set(chosen)
        issued_serials = []
        for stock_row in stock_rows:
            current = cls.ensure_stock_serials(stock_row)
            take = [serial for serial in current if serial in remaining_chosen]
            if not take:
                continue
            take_set = set(take)
            remaining_chosen.difference_update(take_set)
            remaining_serials = [serial for serial in current if serial not in take_set]
            available_quantity = max(int(stock_row.quantity or 0), 0)
            take_quantity = len(take)
            stock_row.quantity = max(available_quantity - take_quantity, 0)
            stock_row.serial_numbers = remaining_serials
            stock_row.issued_serial_numbers = cls.normalize_serials(
                cls.normalize_serials(stock_row.issued_serial_numbers) + take
            )
            stock_row.issued = stock_row.quantity == 0
            stock_row.save(update_fields=[
                "quantity",
                "serial_numbers",
                "issued_serial_numbers",
                "issued",
            ])
            issued_serials.extend(take)

        ordered_issued = [serial for serial in chosen if serial in set(issued_serials)]
        if len(ordered_issued) != requested:
            raise ValueError(
                "In-Store serial deduction was incomplete. No stock was committed."
            )
        return requested, ordered_issued

    @staticmethod
    def normalize_source(value):
        source = str(value or "").strip().upper()

        aliases = {
            "STORE": "STORE",
            "IN_STORE": "STORE",
            "IN-STORE": "STORE",
            "INVENTORY": "STORE",
            "PURCHASED": "PURCHASED",
            "PROJECT": "PURCHASED",
            "PROJECT_INVENTORY": "PURCHASED",
            "QC": "PURCHASED",
            "QC_PASSED": "PURCHASED",
            "ALL": "ALL",
            "BOTH": "ALL",
        }

        return aliases.get(source, source)

    @classmethod
    def get_related_procurement_data(
        cls,
        material_request,
    ):
        canonical_mr_number = str(
            material_request.material_request_id
        ).strip()

        purchase_orders = list(
            PurchaseOrder.objects
            .filter(
                source_mr_number=canonical_mr_number
            )
            .exclude(
                status__in=[
                    "REJECTED",
                    "FINANCE_REJECTED",
                ]
            )
            .prefetch_related(
                "items",
                "items__component",
            )
        )

        purchase_order_ids = [
            row.id
            for row in purchase_orders
        ]

        inwards = list(
            InwardEntry.objects
            .filter(
                purchase_order_id__in=(
                    purchase_order_ids
                )
            )
            .filter(
                Q(removed_from_inventory=False)
                | Q(
                    removed_from_inventory__isnull=True
                )
            )
            .select_related(
                "purchase_order",
                "component",
            )
        )

        return purchase_orders, inwards

    @classmethod
    def synchronize_project_rows(
        cls,
        material_request,
    ):
        """
        Refresh ProjectInventory from reservations and QC.

        This method never marks quantities as issued.
        """
        groups = cls.get_component_groups(
            material_request,
            lock=True,
        )

        purchase_orders, inwards = (
            cls.get_related_procurement_data(
                material_request
            )
        )

        project_rows = []

        for component_id in sorted(groups):
            group = groups[component_id]
            items = group["items"]
            required_quantity = int(
                group["required_quantity"] or 0
            )

            reservation, _ = (
                InventoryReservation.objects
                .select_for_update()
                .get_or_create(
                    material_request=material_request,
                    component_id=component_id,
                    defaults={
                        "requested_quantity": (
                            required_quantity
                        ),
                        "reserved_store_quantity": 0,
                        "procurement_shortage_quantity": (
                            required_quantity
                        ),
                    },
                )
            )

            reservation.requested_quantity = (
                required_quantity
            )

            component_purchase_orders = [
                purchase_order
                for purchase_order in purchase_orders
                if any(
                    str(po_item.component_id)
                    == str(component_id)
                    for po_item
                    in purchase_order.items.all()
                )
            ]

            component_po_ids = {
                purchase_order.id
                for purchase_order
                in component_purchase_orders
            }

            component_inwards = [
                inward
                for inward in inwards
                if (
                    str(inward.component_id)
                    == str(component_id)
                    and inward.purchase_order_id
                    in component_po_ids
                )
            ]

            qc_passed_quantity = sum(
                cls.get_qc_rows_quantity(
                    inward.qc_passed_rows
                )
                for inward in component_inwards
            )

            qc_failed_quantity = sum(
                cls.get_qc_rows_quantity(
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
                for serial in cls.get_inward_passed_serials(inward):
                    if serial not in purchased_serial_seen:
                        purchased_serial_seen.add(serial)
                        purchased_serial_numbers.append(serial)

            store_quantity = min(
                required_quantity,
                int(
                    reservation.reserved_store_quantity
                    or 0
                ),
            )

            purchased_required = max(
                required_quantity - store_quantity,
                0,
            )

            purchased_quantity = min(
                qc_passed_quantity,
                purchased_required,
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
            project_row.store_quantity = store_quantity
            project_row.purchased_quantity = (
                purchased_quantity
            )
            project_row.qc_passed_quantity = (
                qc_passed_quantity
            )
            project_row.qc_failed_quantity = (
                qc_failed_quantity
            )
            project_row.quantity = min(
                required_quantity,
                store_quantity + purchased_quantity,
            )

            project_row.po_numbers = sorted(
                {
                    str(row.po_number)
                    for row in component_purchase_orders
                    if row.po_number
                }
            )

            project_row.inward_codes = sorted(
                {
                    str(row.code)
                    for row in component_inwards
                    if row.code
                }
            )
            project_row.purchased_serial_numbers = (
                purchased_serial_numbers
            )

            project_row.save()

            store_distribution = (
                cls.distribute_quantity(
                    items,
                    store_quantity,
                )
            )

            purchased_distribution = (
                cls.distribute_quantity(
                    items,
                    purchased_quantity,
                )
            )

            qc_failed_distribution = (
                cls.distribute_quantity(
                    items,
                    qc_failed_quantity,
                )
            )

            for item in items:
                changed_fields = []

                values = {
                    "inventory_quantity": (
                        store_distribution.get(
                            item.pk,
                            0,
                        )
                    ),
                    "qc_passed_quantity": (
                        purchased_distribution.get(
                            item.pk,
                            0,
                        )
                    ),
                    "qc_failed_quantity": (
                        qc_failed_distribution.get(
                            item.pk,
                            0,
                        )
                    ),
                }

                for field_name, value in values.items():
                    if (
                        int(
                            getattr(
                                item,
                                field_name,
                                0,
                            )
                            or 0
                        )
                        != value
                    ):
                        setattr(
                            item,
                            field_name,
                            value,
                        )
                        changed_fields.append(field_name)

                if changed_fields:
                    item.save(
                        update_fields=changed_fields
                    )

            project_rows.append(project_row)

        return project_rows

    @staticmethod
    def resolve_project_row(
        project_rows,
        component_reference,
    ):
        reference = str(
            component_reference or ""
        ).strip()

        if not reference:
            return None

        for row in project_rows:
            candidates = {
                str(row.component_id),
                str(
                    getattr(
                        row.component,
                        "component_id",
                        "",
                    )
                ),
                str(
                    getattr(
                        row.component,
                        "name",
                        "",
                    )
                ),
            }

            if reference in candidates:
                return row

        return None

    @classmethod
    def issue_store_quantity(
        cls,
        project_row,
        quantity,
        selected_serials=None,
    ):
        reservation = InventoryReservation.objects.select_for_update().get(
            material_request=project_row.material_request,
            component=project_row.component,
        )
        available = min(
            int(project_row.remaining_store_quantity or 0),
            int(project_row.remaining_quantity or 0),
            int(reservation.remaining_reserved_quantity or 0),
        )

        requested = (
            available
            if quantity is None
            else max(int(quantity or 0), 0)
        )

        if requested > available:
            raise ValueError(
                f"Only {available} reserved In-Store item(s) remain "
                "for this component."
            )

        selected = cls.normalize_serials(selected_serials)
        if selected and len(selected) != requested:
            raise ValueError(
                "Selected In-Store serial count must equal the issue quantity."
            )

        deducted, serials = cls.deduct_store_fifo(
            project_row.component_id,
            requested,
            selected_serials=selected_serials,
        )
        if deducted > 0:
            project_row.issued_store_quantity = (
                int(project_row.issued_store_quantity or 0) + deducted
            )
            project_row.issued_store_serials = cls.normalize_serials(
                cls.normalize_serials(project_row.issued_store_serials) + serials
            )
            reservation.issued_store_quantity = (
                int(reservation.issued_store_quantity or 0) + deducted
            )
            reservation.save()
        return deducted, serials

    @classmethod
    def issue_purchased_quantity(
        cls,
        project_row,
        quantity,
        selected_serials=None,
    ):
        available_quantity = min(
            int(project_row.remaining_purchased_quantity or 0),
            int(project_row.remaining_quantity or 0),
        )

        requested = (
            available_quantity
            if quantity is None
            else max(int(quantity or 0), 0)
        )

        if requested > available_quantity:
            raise ValueError(
                f"Only {available_quantity} QC-passed item(s) remain "
                "for this component."
            )

        issued_before = set(
            cls.normalize_serials(project_row.issued_purchased_serials)
        )
        available_serials = [
            serial
            for serial in cls.normalize_serials(
                project_row.purchased_serial_numbers
            )
            if serial not in issued_before
        ]
        selected = cls.normalize_serials(selected_serials)
        if selected and len(selected) != requested:
            raise ValueError(
                "Selected QC-passed serial count must equal the issue quantity."
            )
        chosen = selected or available_serials[:requested]
        missing = [serial for serial in chosen if serial not in available_serials]
        if missing:
            raise ValueError(
                "One or more selected QC-passed serials are unavailable: "
                + ", ".join(missing)
            )
        if len(chosen) < requested:
            raise ValueError(
                f"Only {len(chosen)} QC-passed serial(s) are available; "
                f"{requested} were requested."
            )
        if requested > 0:
            project_row.issued_purchased_quantity = (
                int(project_row.issued_purchased_quantity or 0) + requested
            )
            project_row.issued_purchased_serials = cls.normalize_serials(
                cls.normalize_serials(project_row.issued_purchased_serials) + chosen
            )
        return requested, chosen

    @classmethod
    def apply_allocation(
        cls,
        project_row,
        *,
        source,
        quantity=None,
        selected_serials=None,
    ):
        normalized_source = cls.normalize_source(source)
        store_issued = 0
        purchased_issued = 0
        store_serials = []
        purchased_serials = []

        if normalized_source == "PURCHASED":
            purchased_issued, purchased_serials = cls.issue_purchased_quantity(
                project_row, quantity, selected_serials=selected_serials
            )
        elif normalized_source == "STORE":
            store_issued, store_serials = cls.issue_store_quantity(
                project_row, quantity, selected_serials=selected_serials
            )
        elif normalized_source == "ALL":
            if selected_serials:
                raise ValueError(
                    "Serial selection requires source STORE or PURCHASED, not ALL."
                )
            remaining_requested = (
                None if quantity is None else max(int(quantity or 0), 0)
            )
            purchased_issued, purchased_serials = cls.issue_purchased_quantity(
                project_row, remaining_requested
            )
            if remaining_requested is not None:
                remaining_requested = max(
                    remaining_requested - purchased_issued, 0
                )
            store_issued, store_serials = cls.issue_store_quantity(
                project_row, remaining_requested
            )
        else:
            raise ValueError("Source must be STORE, PURCHASED, or ALL.")

        project_row.save()
        return {
            "component_id": project_row.component_id,
            "source": normalized_source,
            "issued_store_quantity": store_issued,
            "issued_purchased_quantity": purchased_issued,
            "issued_store_serials": store_serials,
            "issued_purchased_serials": purchased_serials,
            "issued_serials": store_serials + purchased_serials,
            "issued_total": store_issued + purchased_issued,
            "remaining_quantity": project_row.remaining_quantity,
            "status": project_row.status,
        }

    @classmethod
    def update_item_issued_quantities(
        cls,
        material_request,
        project_rows,
    ):
        groups = cls.get_component_groups(
            material_request,
            lock=True,
        )

        rows_by_component = {
            row.component_id: row
            for row in project_rows
        }

        for component_id, group in groups.items():
            project_row = rows_by_component.get(
                component_id
            )

            if project_row is None:
                continue

            distribution = cls.distribute_quantity(
                group["items"],
                project_row.issued_quantity,
            )

            for item in group["items"]:
                value = distribution.get(item.pk, 0)

                if (
                    int(
                        item.project_inventory_quantity
                        or 0
                    )
                    != value
                ):
                    item.project_inventory_quantity = value
                    item.save(
                        update_fields=[
                            "project_inventory_quantity",
                        ]
                    )

    @staticmethod
    def upsert_inventory_notification(
        material_request,
        *,
        notification_status,
        title,
        message,
        is_read,
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

    @action(
        detail=False,
        methods=["post"],
        url_path="sync-mr",
    )
    @transaction.atomic
    def sync_material_request(self, request):
        """
        Synchronize and provide MR components.

        Source-specific request:

            {
                "material_request_id": "MR-...",
                "allocations": [
                    {
                        "component_id": 12,
                        "source": "PURCHASED",
                        "quantity": 7
                    }
                ]
            }

        A single allocation is also accepted:

            {
                "material_request_id": "MR-...",
                "component_id": 12,
                "source": "STORE",
                "quantity": 3
            }

        For compatibility, when no source or allocations are supplied,
        every currently available quantity is provided, PURCHASED first
        and STORE second.
        """
        reference = (
            request.data.get("material_request_id")
            or request.data.get("source_mr_number")
            or request.data.get("reference_id")
            or request.data.get("mr_id")
        )

        material_request = (
            self.resolve_material_request(
                reference,
                lock=True,
            )
        )

        if not material_request:
            return Response(
                {
                    "detail": (
                        "Material Request was not found."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        project_rows = (
            self.synchronize_project_rows(
                material_request
            )
        )

        if not project_rows:
            return Response(
                {
                    "detail": (
                        "Material Request has no valid "
                        "component rows."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Prevent duplicate completion emails if a completed MR is
        # synchronized again without issuing any new quantity.
        was_fully_issued = all(
            row.is_fulfilled
            for row in project_rows
        )

        raw_allocations = request.data.get(
            "allocations"
        )

        if raw_allocations is None:
            source = request.data.get("source")
            component_reference = (
                request.data.get("component_id")
                or request.data.get("component")
                or request.data.get("component_code")
            )
            quantity = request.data.get("quantity")

            if source or component_reference:
                raw_allocations = [
                    {
                        "component_id": (
                            component_reference
                        ),
                        "source": source or "ALL",
                        "quantity": quantity,
                    }
                ]
            else:
                raw_allocations = [
                    {
                        "component_id": (
                            row.component_id
                        ),
                        "source": "ALL",
                        "quantity": None,
                    }
                    for row in project_rows
                ]

        if not isinstance(raw_allocations, list):
            return Response(
                {
                    "detail": (
                        "allocations must be a list."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        issue_summary = []
        seen_allocation_keys = set()

        try:
            for allocation in raw_allocations:
                if not isinstance(allocation, dict):
                    raise ValueError(
                        "Each allocation must be an object."
                    )

                component_reference = (
                    allocation.get("component_id")
                    or allocation.get("component")
                    or allocation.get(
                        "component_code"
                    )
                )

                project_row = self.resolve_project_row(
                    project_rows,
                    component_reference,
                )

                if project_row is None:
                    raise ValueError(
                        "A requested component was not "
                        "found in this Material Request."
                    )

                normalized_source = self.normalize_source(
                    allocation.get("source") or "ALL"
                )

                allocation_key = (
                    int(project_row.component_id),
                    normalized_source,
                )

                if allocation_key in seen_allocation_keys:
                    raise ValueError(
                        "Duplicate allocation received for the same "
                        "component and source. Refresh the Provide "
                        "Components popup and try again."
                    )

                seen_allocation_keys.add(allocation_key)

                raw_quantity = allocation.get(
                    "quantity"
                )
                selected_serials = self.normalize_serials(
                    allocation.get("serial_numbers")
                    or allocation.get("selected_serials")
                    or allocation.get("serials")
                    or []
                )

                quantity = None
                if raw_quantity not in (None, ""):
                    quantity = int(raw_quantity)
                    if quantity < 0:
                        raise ValueError(
                            "Quantity cannot be negative."
                        )
                elif selected_serials:
                    quantity = len(selected_serials)

                if (
                    selected_serials
                    and quantity != len(selected_serials)
                ):
                    raise ValueError(
                        "Allocation quantity must equal the selected serial count."
                    )

                issue_summary.append(
                    self.apply_allocation(
                        project_row,
                        source=normalized_source,
                        quantity=quantity,
                        selected_serials=selected_serials,
                    )
                )
        except (
            TypeError,
            ValueError,
            InventoryReservation.DoesNotExist,
        ) as exc:
            transaction.set_rollback(True)

            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Re-read locked rows after issue calculations.
        project_rows = list(
            ProjectInventory.objects
            .select_for_update()
            .select_related("component")
            .filter(
                material_request=material_request
            )
            .order_by("id")
        )

        self.update_item_issued_quantities(
            material_request,
            project_rows,
        )

        all_fulfilled = all(
            row.is_fulfilled
            for row in project_rows
        )

        any_issued = any(
            int(row.issued_quantity or 0) > 0
            for row in project_rows
        )

        canonical_mr_number = str(
            material_request.material_request_id
        ).strip()

        if all_fulfilled:
            material_request.status = (
                "INVENTORY_ISSUED"
            )
            material_request.save(
                update_fields=["status"]
            )

            # ------------------------------------------------------
            # INVENTORY NOTIFICATION LIFECYCLE
            # ------------------------------------------------------
            # Keep the completed Inventory notification visible.
            #
            # The Inventory user explicitly removes only this notification
            # from the Inventory Notifications page after reviewing that all
            # component rows were issued. The Material Request and Project
            # Inventory history remain untouched.
            self.upsert_inventory_notification(
                material_request,
                notification_status="INVENTORY_ISSUED",
                title=(
                    "All Components Issued - "
                    + canonical_mr_number
                ),
                message=(
                    "Every requested component has been issued for "
                    f"{canonical_mr_number}. Review the completed request "
                    "and use Remove when it no longer needs to remain in "
                    "Inventory Notifications."
                ),
                is_read=False,
            )

            # Final return email -> original MR requester/Engineer.
            # Send once, only on the transition from incomplete to fully issued.
            if not was_fully_issued:
                current_user = getattr(
                    request,
                    "user",
                    None,
                )

                if (
                    current_user
                    and getattr(
                        current_user,
                        "is_authenticated",
                        False,
                    )
                ):
                    issued_by_name = (
                        self.get_user_display_name(
                            current_user,
                            "Inventory Team",
                        )
                    )
                else:
                    issued_by_name = (
                        "Inventory Team"
                    )

                transaction.on_commit(
                    lambda mr_id=material_request.id,
                    issuer=issued_by_name: (
                        self.send_requester_all_components_issued_email(
                            mr_id,
                            issued_by_name=issuer,
                        )
                    )
                )
        else:
            current_status = str(
                material_request.status or ""
            ).strip().upper()

            if current_status in {
                "PO_DELIVERED",
                "QC_CHECKED",
                "PROJECT_INVENTORY_READY",
            }:
                pending_notification_status = (
                    "QC_CHECKED"
                )
            else:
                pending_notification_status = (
                    "INVENTORY_PENDING"
                )

            self.upsert_inventory_notification(
                material_request,
                notification_status=(
                    pending_notification_status
                ),
                title=(
                    (
                        "Components Partially Issued - "
                        if any_issued
                        else "Components Pending - "
                    )
                    + canonical_mr_number
                ),
                message=(
                    (
                        "Some components were provided, "
                        "but remaining quantities are "
                        "still pending for "
                        if any_issued
                        else
                        "Components are ready only in "
                        "part. Remaining quantities are "
                        "still pending for "
                    )
                    + f"{canonical_mr_number}."
                ),
                is_read=False,
            )

        response_serializer = self.get_serializer(
            project_rows,
            many=True,
        )

        return Response(
            {
                "material_request_id": (
                    canonical_mr_number
                ),
                "mr_status": material_request.status,
                "all_fulfilled": all_fulfilled,
                "any_issued": any_issued,
                "issue_summary": issue_summary,
                "project_inventory": (
                    response_serializer.data
                ),
            },
            status=status.HTTP_200_OK,
        )