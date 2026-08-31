from datetime import datetime, timedelta
from uuid import uuid4

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from components.models import Component
from notifications.models import Notification

from inventory.models import (
    Inventory,
    InventoryReservation,
    ProjectInventory,
)
from materialrequest.models import MaterialRequest
from outward.models import OutwardEntry

from .models import ComponentUsage
from .serializers import ComponentUsageSerializer


class ComponentUsageViewSet(ModelViewSet):
    queryset = (
        ComponentUsage.objects
        .select_related("component", "material_request")
        .all()
    )
    serializer_class = ComponentUsageSerializer
    pagination_class = None
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    ACTIVE_RESERVATION_STATUSES = {
        "ACTIVE",
        "PARTIAL",
    }

    # --------------------------------------------------------------
    # SERIAL / STOCK HELPERS
    # --------------------------------------------------------------
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
    def ensure_stock_serials(cls, stock_row):
        serials = cls.normalize_serials(
            stock_row.serial_numbers
        )

        quantity = max(
            int(stock_row.quantity or 0),
            0,
        )

        prefix = "".join(
            character
            for character in str(
                stock_row.inventory_code
                or f"INV{stock_row.pk}"
            )
            if character.isalnum()
        ).upper() or f"INV{stock_row.pk}"

        seen = set(serials) | set(
            cls.normalize_serials(
                stock_row.issued_serial_numbers
            )
        )

        index = 1

        while len(serials) < quantity:
            serial = (
                f"CINV_{prefix}_S{index:05d}"
            )
            index += 1

            if serial in seen:
                continue

            seen.add(serial)
            serials.append(serial)

        normalized = serials[:quantity]

        if normalized != cls.normalize_serials(
            stock_row.serial_numbers
        ):
            stock_row.serial_numbers = normalized
            stock_row.save(
                update_fields=["serial_numbers"]
            )

        return normalized

    @classmethod
    def get_reserved_quantity(
        cls,
        component_id,
        *,
        lock=False,
    ):
        queryset = InventoryReservation.objects.filter(
            component_id=component_id,
            status__in=cls.ACTIVE_RESERVATION_STATUSES,
        )

        if lock:
            queryset = queryset.select_for_update()

        reservations = list(queryset)

        return sum(
            max(
                int(
                    reservation.remaining_reserved_quantity
                    or 0
                ),
                0,
            )
            for reservation in reservations
        )

    @classmethod
    def get_stock_rows(
        cls,
        component_id,
        *,
        lock=False,
    ):
        queryset = (
            Inventory.objects
            .filter(
                component_id=component_id,
                issued=False,
                quantity__gt=0,
            )
            .order_by(
                "received_date",
                "created_at",
                "id",
            )
        )

        if lock:
            queryset = queryset.select_for_update()

        return list(queryset)

    @classmethod
    def build_usage_stock_snapshot(
        cls,
        component_id,
    ):
        stock_rows = cls.get_stock_rows(
            component_id,
            lock=False,
        )

        physical_quantity = sum(
            max(int(row.quantity or 0), 0)
            for row in stock_rows
        )

        reserved_quantity = cls.get_reserved_quantity(
            component_id,
            lock=False,
        )

        available_quantity = max(
            physical_quantity - reserved_quantity,
            0,
        )

        all_serials = []

        for stock_row in stock_rows:
            all_serials.extend(
                cls.ensure_stock_serials(stock_row)
            )

        # Reservations are quantity-based, not serial-specific.
        # Expose only as many serials as the unreserved usage quantity.
        available_serials = all_serials[
            :available_quantity
        ]

        return {
            "physical_quantity": physical_quantity,
            "reserved_quantity": reserved_quantity,
            "available_quantity": available_quantity,
            "available_serials": available_serials,
        }

    @classmethod
    def deduct_usage_stock(
        cls,
        component_id,
        quantity,
        selected_serials=None,
    ):
        requested = max(
            int(quantity or 0),
            0,
        )

        if requested <= 0:
            raise ValueError(
                "Quantity must be greater than 0."
            )

        # Lock reservations and stock rows in the same transaction so
        # Component Usage cannot consume quantity reserved for an MR.
        reserved_quantity = cls.get_reserved_quantity(
            component_id,
            lock=True,
        )

        stock_rows = cls.get_stock_rows(
            component_id,
            lock=True,
        )

        physical_quantity = sum(
            max(int(row.quantity or 0), 0)
            for row in stock_rows
        )

        available_quantity = max(
            physical_quantity - reserved_quantity,
            0,
        )

        if requested > available_quantity:
            raise ValueError(
                (
                    f"Only {available_quantity} unreserved "
                    "In-Store item(s) are available for "
                    "Component Usage. "
                    f"{reserved_quantity} item(s) are protected "
                    "for active Material Requests."
                )
            )

        all_serials = []
        serial_to_row = {}

        for stock_row in stock_rows:
            row_serials = cls.ensure_stock_serials(
                stock_row
            )

            for serial in row_serials:
                if serial not in serial_to_row:
                    serial_to_row[serial] = stock_row
                    all_serials.append(serial)

        # Only the unreserved quantity is selectable for general usage.
        usage_serial_pool = all_serials[
            :available_quantity
        ]
        usage_serial_set = set(
            usage_serial_pool
        )

        selected = cls.normalize_serials(
            selected_serials or []
        )

        if not selected:
            raise ValueError(
                (
                    "Serial Number is mandatory for an "
                    "In-Store Component issue."
                )
            )

        if len(selected) != requested:
            raise ValueError(
                (
                    "Selected serial count must equal "
                    "the issue quantity."
                )
            )

        unavailable = [
            serial
            for serial in selected
            if serial not in usage_serial_set
        ]

        if unavailable:
            raise ValueError(
                (
                    "One or more selected serial numbers "
                    "are unavailable or protected by an "
                    "active Material Request: "
                    + ", ".join(unavailable)
                )
            )

        # Deduct the exact physical serial(s) selected by the user.
        chosen = selected

        if len(chosen) != requested:
            raise ValueError(
                (
                    f"Only {len(chosen)} serial-tracked "
                    "unreserved item(s) are currently available."
                )
            )

        chosen_set = set(chosen)
        issue_details = []
        actual_issued_serials = []

        for stock_row in stock_rows:
            current_serials = cls.ensure_stock_serials(
                stock_row
            )

            taken = [
                serial
                for serial in current_serials
                if serial in chosen_set
            ]

            if not taken:
                continue

            taken_set = set(taken)

            stock_row.serial_numbers = [
                serial
                for serial in current_serials
                if serial not in taken_set
            ]

            stock_row.issued_serial_numbers = (
                cls.normalize_serials(
                    cls.normalize_serials(
                        stock_row.issued_serial_numbers
                    )
                    + taken
                )
            )

            stock_row.quantity = max(
                int(stock_row.quantity or 0)
                - len(taken),
                0,
            )

            stock_row.issued = (
                stock_row.quantity == 0
            )

            stock_row.save(
                update_fields=[
                    "quantity",
                    "serial_numbers",
                    "issued_serial_numbers",
                    "issued",
                ]
            )

            issue_details.append(
                {
                    "inventory_id": stock_row.id,
                    "inventory_code": (
                        stock_row.inventory_code
                        or ""
                    ),
                    "quantity": len(taken),
                    "serial_numbers": taken,
                }
            )

            actual_issued_serials.extend(taken)

        ordered_issued = [
            serial
            for serial in chosen
            if serial in set(
                actual_issued_serials
            )
        ]

        if len(ordered_issued) != requested:
            raise ValueError(
                (
                    "In-Store deduction was incomplete. "
                    "No Component Usage stock change was committed."
                )
            )

        return issue_details, ordered_issued

    @classmethod
    def restore_usage_stock(
        cls,
        usage,
    ):
        issue_details = (
            usage.inventory_issue_details
            if isinstance(
                usage.inventory_issue_details,
                list,
            )
            else []
        )

        if not issue_details:
            raise ValueError(
                (
                    "This usage record does not contain "
                    "Inventory issue details and cannot be "
                    "automatically returned."
                )
            )

        for detail in issue_details:
            inventory_id = detail.get(
                "inventory_id"
            )

            if not inventory_id:
                raise ValueError(
                    (
                        "A source Inventory row is missing "
                        "from the usage audit trail."
                    )
                )

            stock_row = (
                Inventory.objects
                .select_for_update()
                .filter(pk=inventory_id)
                .first()
            )

            if stock_row is None:
                raise ValueError(
                    (
                        f"Inventory row {inventory_id} "
                        "no longer exists. Return cannot "
                        "be completed automatically."
                    )
                )

            serials = cls.normalize_serials(
                detail.get(
                    "serial_numbers",
                    [],
                )
            )

            quantity = max(
                int(
                    detail.get("quantity")
                    or len(serials)
                    or 0
                ),
                0,
            )

            current_serials = cls.normalize_serials(
                stock_row.serial_numbers
            )

            issued_serials = (
                cls.normalize_serials(
                    stock_row.issued_serial_numbers
                )
            )

            returned_set = set(serials)

            stock_row.serial_numbers = (
                cls.normalize_serials(
                    current_serials + serials
                )
            )

            stock_row.issued_serial_numbers = [
                serial
                for serial in issued_serials
                if serial not in returned_set
            ]

            stock_row.quantity = (
                max(
                    int(stock_row.quantity or 0),
                    0,
                )
                + quantity
            )

            stock_row.issued = False

            stock_row.save(
                update_fields=[
                    "quantity",
                    "serial_numbers",
                    "issued_serial_numbers",
                    "issued",
                ]
            )

    @staticmethod
    def get_active_role(request):
        token = getattr(request, "auth", None)
        token_role = ""
        if token is not None:
            try:
                token_role = str(token.get("active_role", "") or "").strip().lower()
            except (AttributeError, TypeError, ValueError):
                token_role = ""
        return token_role or str(getattr(request.user, "role", "") or "").strip().lower()

    @classmethod
    def require_role(cls, request, allowed_roles):
        role = cls.get_active_role(request)
        if role not in set(allowed_roles):
            raise PermissionError(
                f"This action requires one of these roles: {', '.join(sorted(allowed_roles))}."
            )
        return role

    @staticmethod
    def upsert_return_notification(usage, *, receiver, status_value, title, message):
        reference_id = str(usage.pk)
        queryset = Notification.objects.filter(
            category="CU",
            reference_id=reference_id,
            receiver=receiver,
        ).order_by("-id")
        notification = queryset.first()
        if notification is None:
            notification = Notification.objects.create(
                category="CU",
                title=title,
                message=message,
                reference_id=reference_id,
                receiver=receiver,
                status=status_value,
                is_read=False,
            )
        else:
            notification.title = title
            notification.message = message
            notification.status = status_value
            notification.is_read = False
            notification.save(update_fields=["title", "message", "status", "is_read"])
        queryset.exclude(pk=notification.pk).delete()
        return notification

    @staticmethod
    def _resolve_material_request(reference, *, lock=False):
        raw = str(reference or "").strip()
        if not raw:
            return None

        queryset = MaterialRequest.objects.all()
        if lock:
            queryset = queryset.select_for_update()

        if raw.isdigit():
            match = queryset.filter(pk=int(raw)).first()
            if match is not None:
                return match

        return queryset.filter(
            material_request_id=raw
        ).first()

    @classmethod
    def _issued_project_serials(cls, project_row):
        """
        Return the exact serials that are currently shown in Inventory ->
        In Drone for this ProjectInventory row.

        Current rows persist issued_store_serials / issued_purchased_serials.
        Older rows may have only purchased_serial_numbers plus the issued
        quantity.  The frontend In-Drone popup already uses that compatibility
        fallback, so Returnable must use the same source instead of showing '-'.
        """
        store_serials = cls.normalize_serials(
            getattr(project_row, "issued_store_serials", [])
        )

        purchased_serials = cls.normalize_serials(
            getattr(project_row, "issued_purchased_serials", [])
        )

        if not purchased_serials:
            try:
                issued_purchased_quantity = max(
                    int(
                        getattr(
                            project_row,
                            "issued_purchased_quantity",
                            0,
                        )
                        or 0
                    ),
                    0,
                )
            except (TypeError, ValueError):
                issued_purchased_quantity = 0

            if issued_purchased_quantity > 0:
                purchased_pool = cls.normalize_serials(
                    getattr(
                        project_row,
                        "purchased_serial_numbers",
                        [],
                    )
                )
                available_purchased = set(
                    cls.normalize_serials(
                        getattr(
                            project_row,
                            "available_purchased_serials",
                            [],
                        )
                    )
                )

                # Best legacy reconstruction: serials no longer available are
                # the ones already issued to the MR / In Drone.
                derived_issued = [
                    serial
                    for serial in purchased_pool
                    if serial not in available_purchased
                ]

                purchased_serials = (
                    derived_issued[:issued_purchased_quantity]
                    if derived_issued
                    else purchased_pool[:issued_purchased_quantity]
                )

        return cls.normalize_serials(
            store_serials + purchased_serials
        )

    @classmethod
    def _hydrate_missing_usage_serials(cls, rows):
        """
        Repair legacy ComponentUsage rows that were created without
        issued_serial_numbers.

        Exact serials are recovered from the same MaterialRequest's
        ProjectInventory issued_store_serials + issued_purchased_serials.
        Existing serial assignments from other usages/Sales are excluded.
        No synthetic/fake serial number is created.
        """
        rows = [
            row for row in (rows or [])
            if row is not None
        ]
        if not rows:
            return rows

        material_request = getattr(
            rows[0],
            "material_request",
            None,
        )
        if material_request is None:
            return rows

        row_ids = {
            int(row.pk)
            for row in rows
            if getattr(row, "pk", None)
        }

        project_rows = list(
            ProjectInventory.objects
            .select_for_update()
            .select_related("component")
            .filter(
                material_request=material_request,
            )
            .order_by("id")
        )

        serial_pool = {}
        for project_row in project_rows:
            component_id = getattr(
                project_row,
                "component_id",
                None,
            )
            if not component_id:
                continue

            serial_pool.setdefault(
                component_id,
                [],
            )
            serial_pool[component_id] = (
                cls.normalize_serials(
                    serial_pool[component_id]
                    + cls._issued_project_serials(
                        project_row
                    )
                )
            )

        used = {}

        # Only ACTIVE other Returnable movements reserve an issued serial.
        # A previously returned-good / completed movement released its serial
        # back to In Drone and must NOT block hydration of a later movement.
        other_usages = (
            ComponentUsage.objects
            .select_for_update()
            .filter(
                material_request=material_request,
            )
            .exclude(
                pk__in=row_ids,
            )
            .exclude(
                return_approval_status="REJECTED",
            )
            .exclude(
                return_condition="OK",
                return_approval_status="COMPLETED",
            )
        )

        for other in other_usages:
            component_id = getattr(
                other,
                "component_id",
                None,
            )
            if not component_id:
                continue

            used.setdefault(
                component_id,
                set(),
            ).update(
                cls.normalize_serials(
                    getattr(
                        other,
                        "issued_serial_numbers",
                        [],
                    )
                )
            )

        sales_rows = (
            OutwardEntry.objects
            .select_for_update()
            .filter(
                material_request=material_request,
                outward_type="SALES",
            )
            .exclude(
                approval_status__in={
                    "MANAGEMENT_REJECTED",
                    "REJECTED",
                    "FINANCE_REJECTED",
                }
            )
        )

        for sale in sales_rows:
            component_id = getattr(
                sale,
                "component_id",
                None,
            )
            if not component_id:
                continue

            used.setdefault(
                component_id,
                set(),
            ).update(
                cls.normalize_serials(
                    getattr(
                        sale,
                        "serial_numbers",
                        [],
                    )
                )
            )

        for row in rows:
            component_id = getattr(
                row,
                "component_id",
                None,
            )
            if not component_id:
                continue

            existing = cls.normalize_serials(
                getattr(
                    row,
                    "issued_serial_numbers",
                    [],
                )
            )

            required = max(
                int(
                    getattr(
                        row,
                        "quantity",
                        0,
                    )
                    or 0
                ),
                0,
            )

            if required <= 0:
                continue

            component_used = used.setdefault(
                component_id,
                set(),
            )

            # Preserve any serial already saved on this row.
            component_used.update(existing)

            missing_count = max(
                required - len(existing),
                0,
            )
            if missing_count <= 0:
                continue

            candidates = [
                serial
                for serial in serial_pool.get(
                    component_id,
                    [],
                )
                if serial not in component_used
            ]

            recovered = candidates[
                :missing_count
            ]

            if not recovered:
                continue

            row.issued_serial_numbers = (
                cls.normalize_serials(
                    existing + recovered
                )
            )
            row.save(
                update_fields=[
                    "issued_serial_numbers",
                ]
            )

            component_used.update(
                recovered
            )

        return rows

    @staticmethod
    def _material_request_items(material_request):
        request_type = str(
            material_request.request_type or ""
        ).strip().upper()

        if request_type in {"R&D", "RD"}:
            return list(
                material_request.rd_items
                .select_related("component")
                .all()
                .order_by("id")
            )

        if request_type in {
            "RETAIL_SALES",
            "RETURNABLE",
        } and hasattr(material_request, "request_items"):
            return list(
                material_request.request_items
                .select_related("component")
                .all()
                .order_by("id")
            )

        return list(
            material_request.bom_items
            .select_related("component")
            .all()
            .order_by("id")
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="move-from-in-drone",
    )
    @transaction.atomic
    def move_from_in_drone(self, request):
        """
        Allocate only the requested quantity from an already-issued In-Drone MR
        to Flight Test / Demo-Trials / Event.

        Important behavior:
        - The original MR always remains in In Drone.
        - Central Inventory is NOT deducted again.
        - Multiple simultaneous usages are allowed while quantity is available.
        - A returned-good usage releases its quantity back to In Drone.
        - Manager-rejected usage does not consume In-Drone quantity.
        """
        try:
            self.require_role(
                request,
                {"inventory", "engineer", "admin"},
            )
        except PermissionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )

        material_request = self._resolve_material_request(
            request.data.get("material_request_id")
            or request.data.get("material_request")
            or request.data.get("mr_id")
            or request.data.get("reference_id"),
            lock=True,
        )

        if material_request is None:
            return Response(
                {"detail": "Material Request was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        request_type = str(
            material_request.request_type or ""
        ).strip().upper()

        if request_type == "RETURNABLE":
            return Response(
                {
                    "detail": (
                        "Returnable Material Requests are not moved "
                        "from In Drone using this action."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        mr_status = str(
            material_request.status or ""
        ).strip().upper()

        if mr_status not in {
            "INVENTORY_ISSUED",
            "MR_COMPLETED",
            "ISSUED",
            "COMPLETED",
        }:
            return Response(
                {
                    "detail": (
                        "Only a fully issued In-Drone Material Request "
                        "can be moved to Returnable."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        purpose = str(
            request.data.get("purpose") or ""
        ).strip().upper()

        allowed_purposes = {
            "FLIGHT_TEST",
            "EVENT",
            "CUSTOMER_DEMO",
        }

        if purpose not in allowed_purposes:
            return Response(
                {
                    "purpose": (
                        "Purpose must be Flight Test, Event, "
                        "or Demo/Trials."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_quantity = (
            request.data.get("quantity")
            or request.data.get("usage_quantity")
            or request.data.get("usageQuantity")
        )
        try:
            requested_quantity = int(raw_quantity)
        except (TypeError, ValueError):
            requested_quantity = 0

        if requested_quantity <= 0:
            return Response(
                {"quantity": "Quantity must be at least 1."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_return_date = str(
            request.data.get("return_due_date")
            or request.data.get("return_date")
            or ""
        ).strip()

        if not raw_return_date:
            return Response(
                {"return_due_date": "Returnable Date is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            return_due_date = datetime.strptime(
                raw_return_date,
                "%Y-%m-%d",
            ).date()
        except ValueError:
            return Response(
                {
                    "return_due_date": (
                        "Returnable Date must use YYYY-MM-DD format."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        requested_date = timezone.localdate()

        if return_due_date < requested_date:
            return Response(
                {
                    "return_due_date": (
                        "Returnable Date cannot be before today."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            purpose == "FLIGHT_TEST"
            and return_due_date
            > requested_date + timedelta(days=4)
        ):
            return Response(
                {
                    "return_due_date": (
                        "Flight Test Returnable Date cannot exceed "
                        "4 days from today."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        remarks = str(
            request.data.get("remarks") or ""
        ).strip()

        # Rows still physically unavailable to In Drone.
        active_usage_rows = list(
            ComponentUsage.objects
            .select_for_update()
            .filter(material_request=material_request)
            .exclude(return_approval_status="REJECTED")
            .exclude(
                return_condition="OK",
                return_approval_status="COMPLETED",
            )
        )

        # Pending/approved Sales also reserve permanent In-Drone quantity.
        active_sales_rows = list(
            OutwardEntry.objects
            .select_for_update()
            .filter(
                material_request=material_request,
                outward_type="SALES",
            )
            .exclude(
                approval_status__in={
                    "MANAGEMENT_REJECTED",
                    "REJECTED",
                    "FINANCE_REJECTED",
                }
            )
            .exclude(
                status__in={
                    "MANAGEMENT_REJECTED",
                    "REJECTED",
                    "FINANCE_REJECTED",
                }
            )
        )

        project_rows = list(
            ProjectInventory.objects
            .select_for_update()
            .select_related("component")
            .filter(material_request=material_request)
            .order_by("id")
        )

        movement_id = uuid4().hex
        created_rows = []

        def usage_quantity_for_component(component_id):
            return sum(
                max(int(getattr(row, "quantity", 0) or 0), 0)
                for row in active_usage_rows
                if getattr(row, "component_id", None) == component_id
            )

        def sales_quantity_for_component(component_id):
            return sum(
                max(int(getattr(row, "quantity", 0) or 0), 0)
                for row in active_sales_rows
                if getattr(row, "component_id", None) == component_id
            )

        def allocated_serials_for_component(component_id):
            allocated = set()
            for row in active_usage_rows:
                if getattr(row, "component_id", None) == component_id:
                    allocated.update(
                        self.normalize_serials(
                            getattr(row, "issued_serial_numbers", [])
                        )
                    )
            for row in active_sales_rows:
                if getattr(row, "component_id", None) == component_id:
                    allocated.update(
                        self.normalize_serials(
                            getattr(row, "serial_numbers", [])
                        )
                    )
            return allocated

        for project_row in project_rows:
            issued_quantity = max(
                int(
                    getattr(project_row, "issued_store_quantity", 0)
                    or 0
                )
                + int(
                    getattr(project_row, "issued_purchased_quantity", 0)
                    or 0
                ),
                0,
            )

            if issued_quantity <= 0:
                continue

            component = project_row.component
            component_id = getattr(component, "pk", None)
            already_allocated = (
                usage_quantity_for_component(component_id)
                + sales_quantity_for_component(component_id)
            )
            available_quantity = max(
                issued_quantity - already_allocated,
                0,
            )

            if requested_quantity > available_quantity:
                transaction.set_rollback(True)
                return Response(
                    {
                        "detail": (
                            f"Only {available_quantity} unit(s) are available "
                            f"in In Drone for {getattr(component, 'name', 'this component')}."
                        ),
                        "available_quantity": available_quantity,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            issued_serials = self._issued_project_serials(
                project_row
            )
            allocated_serials = allocated_serials_for_component(
                component_id
            )
            available_serials = [
                serial
                for serial in issued_serials
                if serial not in allocated_serials
            ]

            selected_serials = (
                available_serials[:requested_quantity]
                if available_serials
                else []
            )

            if (
                issued_serials
                and len(selected_serials) < requested_quantity
            ):
                transaction.set_rollback(True)
                return Response(
                    {
                        "detail": (
                            "Not enough unallocated issued serial numbers "
                            "are available for the requested quantity."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            created_rows.append(
                ComponentUsage.objects.create(
                    material_request=material_request,
                    employee_name=(
                        material_request.requester_name
                        or "In Drone"
                    ),
                    item_source="INVENTORY",
                    component=component,
                    component_name=(
                        getattr(component, "name", "")
                        or ""
                    ),
                    component_type=(
                        getattr(component, "category", "")
                        or ""
                    ),
                    purpose=purpose,
                    requested_date=requested_date,
                    return_due_date=return_due_date,
                    issued_date=requested_date,
                    quantity=requested_quantity,
                    status="PENDING",
                    remarks=remarks,
                    return_approval_status="PENDING_MANAGER",
                    issued_serial_numbers=selected_serials,
                    inventory_issue_details=[
                        {
                            "source": "IN_DRONE",
                            "movement_id": movement_id,
                            "quantity": requested_quantity,
                        }
                    ],
                    inventory_adjusted=False,
                    inventory_returned=False,
                )
            )

        # Legacy / From-Scrap completed MRs without ProjectInventory rows.
        if not project_rows:
            items = self._material_request_items(material_request)
            for item in items:
                component = getattr(item, "component", None)
                total_quantity = max(
                    int(getattr(item, "quantity", 0) or 0),
                    0,
                )
                if component is None or total_quantity <= 0:
                    continue

                component_id = getattr(component, "pk", None)
                available_quantity = max(
                    total_quantity
                    - usage_quantity_for_component(component_id)
                    - sales_quantity_for_component(component_id),
                    0,
                )

                if requested_quantity > available_quantity:
                    transaction.set_rollback(True)
                    return Response(
                        {
                            "detail": (
                                f"Only {available_quantity} unit(s) are available "
                                "for this In-Drone request."
                            ),
                            "available_quantity": available_quantity,
                        },
                        status=status.HTTP_409_CONFLICT,
                    )

                created_rows.append(
                    ComponentUsage.objects.create(
                        material_request=material_request,
                        employee_name=(
                            material_request.requester_name
                            or "In Drone"
                        ),
                        item_source="INVENTORY",
                        component=component,
                        component_name=(
                            getattr(component, "name", "") or ""
                        ),
                        component_type=(
                            getattr(component, "category", "") or ""
                        ),
                        purpose=purpose,
                        requested_date=requested_date,
                        return_due_date=return_due_date,
                        issued_date=requested_date,
                        quantity=requested_quantity,
                        status="PENDING",
                        remarks=remarks,
                        return_approval_status="PENDING_MANAGER",
                        issued_serial_numbers=[],
                        inventory_issue_details=[
                            {
                                "source": "IN_DRONE",
                                "movement_id": movement_id,
                                "quantity": requested_quantity,
                            }
                        ],
                        inventory_adjusted=False,
                        inventory_returned=False,
                    )
                )

        if not created_rows:
            transaction.set_rollback(True)
            return Response(
                {
                    "detail": (
                        "No issued components were found for this "
                        "In-Drone Material Request."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        first_usage = created_rows[0]
        purpose_label = {
            "FLIGHT_TEST": "Flight Test",
            "CUSTOMER_DEMO": "Demo/Trials",
            "EVENT": "Event",
        }.get(purpose, purpose.replace("_", " ").title())

        self.upsert_return_notification(
            first_usage,
            receiver="MANAGER",
            status_value="PENDING_MANAGER",
            title=(
                f"Drone Usage Approval - "
                f"{material_request.material_request_id} - {purpose_label}"
            ),
            message=(
                f"{material_request.material_request_id} is already in In Drone. "
                f"Inventory requested {purpose_label} usage for "
                f"{requested_quantity} unit(s). Manager approval is required."
            ),
        )

        return Response(
            {
                "detail": "Drone usage submitted for Manager approval.",
                "material_request_id": material_request.material_request_id,
                "purpose": purpose,
                "quantity": requested_quantity,
                "movement_id": movement_id,
                "return_due_date": return_due_date,
                "rows": self.get_serializer(
                    created_rows,
                    many=True,
                ).data,
            },
            status=status.HTTP_201_CREATED,
        )


    @action(detail=True, methods=["post"], url_path="usage-approval")
    @transaction.atomic
    def usage_approval(self, request, pk=None):
        """
        Manager approval for Flight Test / Demo-Trials / Event started from
        Inventory -> In Drone.

        IMPORTANT:
        - No new MaterialRequest is created.
        - The original MR number is preserved.
        - The original MR remains in In Drone.
        - No central stock is deducted again because the drone/components were
          already issued before this temporary usage request was created.
        """
        try:
            self.require_role(request, {"manager", "admin"})
        except PermissionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )

        usage = (
            ComponentUsage.objects
            .select_for_update()
            .select_related("material_request")
            .filter(pk=pk)
            .first()
        )
        if usage is None:
            return Response(
                {"detail": "Returnable usage record was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        purpose = str(usage.purpose or "").strip().upper()
        if purpose not in {"FLIGHT_TEST", "CUSTOMER_DEMO", "EVENT"}:
            return Response(
                {
                    "detail": (
                        "This approval endpoint is only for Flight Test, "
                        "Demo/Trials, and Event requests created from In Drone."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        decision = str(
            request.data.get("decision") or ""
        ).strip().upper()

        if decision not in {"APPROVE", "REJECT"}:
            return Response(
                {"decision": "Decision must be APPROVE or REJECT."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # IMPORTANT:
        # ComponentUsage.save() derives status from dates. In-Drone temporary
        # usage rows already have issued_date, therefore their model status is
        # "ISSUED" even while Manager approval is still pending. The previous
        # query incorrectly required status="PENDING", which caused a false
        # 409 Conflict for every valid Flight Test / Demo / Event approval.
        #
        # return_approval_status is the authoritative Manager workflow field.
        movement_rows = self._movement_rows(
            usage,
            lock=True,
        )

        row_ids = [
            row.pk
            for row in movement_rows
            if str(
                row.return_approval_status or ""
            ).strip().upper() == "PENDING_MANAGER"
            and not row.received_date
        ]

        rows = (
            ComponentUsage.objects
            .select_for_update()
            .filter(
                pk__in=row_ids,
                return_approval_status="PENDING_MANAGER",
            )
            .order_by("id")
        )

        if not rows.exists():
            return Response(
                {
                    "detail": (
                        "This Returnable movement is not pending Manager "
                        "approval anymore. Refresh Notifications to see the "
                        "latest status."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        notification = (
            Notification.objects
            .filter(
                category="CU",
                receiver="MANAGER",
                reference_id=str(usage.pk),
            )
            .order_by("-id")
            .first()
        )

        if decision == "REJECT":
            reason = str(
                request.data.get("reason") or ""
            ).strip()

            rows.update(
                return_approval_status="REJECTED",
            )

            if notification is not None:
                notification.status = "MANAGER_REJECTED"
                notification.is_read = True
                if reason:
                    notification.message = (
                        f"{notification.message or ''} "
                        f"Rejected reason: {reason}"
                    ).strip()
                notification.save(
                    update_fields=[
                        "status",
                        "is_read",
                        "message",
                    ]
                )

            return Response(
                {
                    "detail": "Drone usage request rejected by Manager.",
                    "status": "MANAGER_REJECTED",
                },
                status=status.HTTP_200_OK,
            )

        # Approved: usage is now active. Reset return_approval_status because
        # that field is later reused only if the returned item is marked NOT OK.
        rows.update(
            status="ISSUED",
            return_approval_status="NOT_REQUIRED",
        )

        if notification is not None:
            notification.status = "MANAGER_APPROVED"
            notification.is_read = True
            notification.save(
                update_fields=["status", "is_read"]
            )

        return Response(
            {
                "detail": "Drone usage approved by Manager.",
                "status": "ISSUED",
                "material_request_id": (
                    usage.material_request.material_request_id
                    if usage.material_request_id
                    else ""
                ),
                "purpose": purpose,
            },
            status=status.HTTP_200_OK,
        )

    # --------------------------------------------------------------
    # RETURNABLE MR SYNC + ENGINEER RETURN + INVENTORY SERIAL QC
    # --------------------------------------------------------------
    @staticmethod
    def _usage_metadata_list(usage):
        details = getattr(usage, "inventory_issue_details", None)
        if isinstance(details, list):
            return [
                dict(item)
                for item in details
                if isinstance(item, dict)
            ]
        if isinstance(details, dict):
            return [dict(details)]
        return []

    @classmethod
    def _usage_metadata(cls, usage):
        merged = {}
        for item in cls._usage_metadata_list(usage):
            merged.update(item)
        return merged

    @classmethod
    def _set_usage_metadata(cls, usage, **values):
        details = cls._usage_metadata_list(usage)
        if not details:
            details = [{}]
        details[0].update(values)
        usage.inventory_issue_details = details
        return details

    @classmethod
    def _movement_id_for_usage(cls, usage):
        return str(
            cls._usage_metadata(usage).get("movement_id") or ""
        ).strip()

    @classmethod
    def _movement_rows(cls, usage, *, lock=False):
        queryset = ComponentUsage.objects.select_related(
            "component", "material_request"
        ).filter(
            material_request=usage.material_request,
            purpose=usage.purpose,
        ).order_by("id")
        if lock:
            queryset = queryset.select_for_update()

        candidates = list(queryset)
        movement_id = cls._movement_id_for_usage(usage)
        if movement_id:
            matched = [
                row
                for row in candidates
                if cls._movement_id_for_usage(row) == movement_id
            ]
            if matched:
                return matched

        # Legacy fallback when movement_id did not exist yet.
        matched = [
            row
            for row in candidates
            if row.requested_date == usage.requested_date
            and row.return_due_date == usage.return_due_date
            and row.received_date == usage.received_date
        ]
        return matched or [usage]

    @staticmethod
    def _actor_name(user):
        if not user:
            return "Engineer"
        try:
            full_name = str(user.get_full_name() or "").strip()
        except Exception:
            full_name = ""
        return (
            full_name
            or str(getattr(user, "name", "") or "").strip()
            or str(getattr(user, "email", "") or "").strip()
            or str(user)
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="sync-returnable-mr",
    )
    @transaction.atomic
    def sync_returnable_mr(self, request):
        """
        Idempotently create ComponentUsage rows after a COMPONENT-mode
        RETURNABLE MR is fully issued. The same MR and issued serials are kept.
        """
        try:
            self.require_role(
                request,
                {"engineer", "inventory", "admin"},
            )
        except PermissionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )

        material_request = self._resolve_material_request(
            request.data.get("material_request_id")
            or request.data.get("material_request")
            or request.data.get("mr_id"),
            lock=True,
        )
        if material_request is None:
            return Response(
                {"detail": "Material Request was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        request_type = str(
            material_request.request_type or ""
        ).strip().upper()
        if request_type != "RETURNABLE":
            return Response(
                {"detail": "Only RETURNABLE Material Requests can be synchronized."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        mr_status = str(
            material_request.status or ""
        ).strip().upper()
        if mr_status not in {
            "INVENTORY_ISSUED",
            "MR_COMPLETED",
            "ISSUED",
            "COMPLETED",
        }:
            return Response(
                {"detail": "Returnable MR has not been fully issued yet."},
                status=status.HTTP_409_CONFLICT,
            )

        purpose = str(
            getattr(material_request, "returnable_purpose", "")
            or "MISCELLANEOUS_USAGE"
        ).strip().upper()

        existing = list(
            ComponentUsage.objects
            .select_for_update()
            .filter(material_request=material_request)
            .order_by("id")
        )
        if existing:
            self._hydrate_missing_usage_serials(
                existing
            )
            return Response(
                self.get_serializer(existing, many=True).data,
                status=status.HTTP_200_OK,
            )

        project_rows = list(
            ProjectInventory.objects
            .select_for_update()
            .select_related("component")
            .filter(material_request=material_request)
            .order_by("id")
        )
        if not project_rows:
            return Response(
                {"detail": "No issued Project Inventory rows were found for this Returnable MR."},
                status=status.HTTP_409_CONFLICT,
            )

        movement_id = f"RETURNABLE-MR-{material_request.pk}-{uuid4().hex[:10]}"
        created = []
        for project_row in project_rows:
            issued_quantity = max(
                int(getattr(project_row, "issued_store_quantity", 0) or 0)
                + int(getattr(project_row, "issued_purchased_quantity", 0) or 0),
                0,
            )
            if issued_quantity <= 0:
                continue

            serials = self.normalize_serials(
                self.normalize_serials(
                    getattr(project_row, "issued_store_serials", [])
                )
                + self.normalize_serials(
                    getattr(project_row, "issued_purchased_serials", [])
                )
            )
            component = project_row.component
            created.append(
                ComponentUsage.objects.create(
                    material_request=material_request,
                    employee_name=(
                        material_request.requester_name
                        or "Engineer"
                    ),
                    item_source="INVENTORY",
                    component=component,
                    component_name=(
                        getattr(component, "name", "") or ""
                    ),
                    component_type=(
                        getattr(component, "category", "") or ""
                    ),
                    purpose=purpose,
                    requested_date=timezone.localdate(),
                    return_due_date=getattr(
                        material_request,
                        "required_date",
                        None,
                    ),
                    issued_date=timezone.localdate(),
                    quantity=issued_quantity,
                    status="ISSUED",
                    remarks=(
                        getattr(material_request, "remarks", "")
                        or ""
                    ),
                    return_approval_status="NOT_REQUIRED",
                    issued_serial_numbers=serials,
                    inventory_issue_details=[
                        {
                            "source": "RETURNABLE_MR",
                            "movement_id": movement_id,
                            "mode": "COMPONENT",
                            "quantity": issued_quantity,
                        }
                    ],
                    inventory_adjusted=False,
                    inventory_returned=False,
                )
            )

        if not created:
            return Response(
                {"detail": "No issued component quantities were available to synchronize."},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            self.get_serializer(created, many=True).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="engineer-return",
    )
    @transaction.atomic
    def engineer_return(self, request, pk=None):
        """
        Engineer hand-back only. No QC decision is accepted here.

        Engineer supplies Return Date + optional Remarks. Every row belonging
        to the same movement is moved into Inventory -> Returned, and Inventory
        receives one QC-pending notification.
        """
        try:
            self.require_role(request, {"engineer", "admin"})
        except PermissionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )

        usage = (
            ComponentUsage.objects
            .select_for_update()
            .select_related("component", "material_request")
            .filter(pk=pk)
            .first()
        )
        if usage is None:
            return Response(
                {"detail": "Returnable usage record was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        raw_date = str(
            request.data.get("return_date")
            or request.data.get("received_date")
            or ""
        ).strip()
        if not raw_date:
            return Response(
                {"return_date": "Return Date is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return_date = datetime.strptime(
                raw_date,
                "%Y-%m-%d",
            ).date()
        except ValueError:
            return Response(
                {"return_date": "Return Date must use YYYY-MM-DD format."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if return_date > timezone.localdate():
            return Response(
                {"return_date": "Return Date cannot be in the future."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        remarks = str(request.data.get("remarks") or "").strip()
        rows = self._movement_rows(usage, lock=True)
        self._hydrate_missing_usage_serials(
            rows
        )
        rows = [
            row for row in rows
            if row.issued_date and not row.received_date
        ]
        if not rows:
            return Response(
                {"detail": "This movement has already been returned or is not issued."},
                status=status.HTTP_409_CONFLICT,
            )

        actor = self._actor_name(request.user)
        movement_id = self._movement_id_for_usage(usage)
        for row in rows:
            self._set_usage_metadata(
                row,
                movement_id=(
                    self._movement_id_for_usage(row)
                    or movement_id
                ),
                returned_by=actor,
                return_date=raw_date,
                return_remarks=remarks,
                return_qc_status="PENDING",
            )
            row.received_date = return_date
            row.return_condition = ""
            row.return_reason = ""
            row.return_approval_status = "NOT_REQUIRED"
            row.save(
                update_fields=[
                    "received_date",
                    "return_condition",
                    "return_reason",
                    "return_approval_status",
                    "inventory_issue_details",
                ]
            )

        first = rows[0]
        mr_number = (
            getattr(first.material_request, "material_request_id", "")
            or "Returnable"
        )
        purpose_label = str(first.purpose or "Returnable").replace("_", " ").title()

        notification, _ = Notification.objects.update_or_create(
            category="CU",
            receiver="INVENTORY",
            reference_id=str(first.pk),
            defaults={
                "requested_by": actor,
                "title": f"Return QC Required - {mr_number}",
                "message": (
                    f"{actor} moved {mr_number} ({purpose_label}) to Returned "
                    f"on {raw_date}. Inventory serial-level QC is required."
                ),
                "status": "INVENTORY_CHECK_PENDING",
                "is_read": False,
            },
        )

        return Response(
            {
                "detail": "Moved to Inventory Returned. QC is pending.",
                "material_request_id": mr_number,
                "movement_id": movement_id,
                "return_date": raw_date,
                "rows": self.get_serializer(rows, many=True).data,
                "notification_id": notification.pk,
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="hydrate-return-serials",
    )
    @transaction.atomic
    def hydrate_return_serials(
        self,
        request,
        pk=None,
    ):
        """
        Inventory/Admin helper used when opening Returnable QC.

        It repairs missing historical issued serials from ProjectInventory
        and returns the complete movement rows. No QC decision is made here.
        """
        try:
            self.require_role(
                request,
                {
                    "inventory",
                    "admin",
                    "engineer",
                    "manager",
                    "procurement",
                },
            )
        except PermissionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )

        usage = (
            ComponentUsage.objects
            .select_for_update()
            .select_related(
                "component",
                "material_request",
            )
            .filter(pk=pk)
            .first()
        )
        if usage is None:
            return Response(
                {
                    "detail": (
                        "Returned usage record was not found."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # View Details needs the exact In-Drone serials at every workflow
        # stage, not only after Engineer clicks Move to Return.  QC itself is
        # still protected by return_qc(), which requires returned rows.
        rows = list(
            self._movement_rows(
                usage,
                lock=True,
            )
        )

        if not rows:
            return Response(
                {
                    "detail": (
                        "No Returnable movement rows were found."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        self._hydrate_missing_usage_serials(
            rows
        )

        return Response(
            {
                "rows": self.get_serializer(
                    rows,
                    many=True,
                ).data,
                "missing_serial_units": sum(
                    max(
                        int(row.quantity or 0)
                        - len(
                            self.normalize_serials(
                                row.issued_serial_numbers
                            )
                        ),
                        0,
                    )
                    for row in rows
                ),
            },
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _returnable_purpose_label(value):
        normalized = str(value or "").strip().upper()
        labels = {
            "FLIGHT_TEST": "Flight Test",
            "CUSTOMER_DEMO": "Demo / Trials",
            "EVENT": "Event",
            "QC_CHECK": "QC Check",
            "MISCELLANEOUS_USAGE": "Miscellaneous Usage",
        }
        return labels.get(
            normalized,
            normalized.replace("_", " ").title() or "Returnable",
        )

    @classmethod
    def _returnable_qc_component_items(
        cls,
        rows,
        normalized_by_usage,
        *,
        condition,
    ):
        """
        Group serial/unit-level Return QC results by component.

        Output is intentionally compatible with the existing Outward Scrap
        metadata used by Manager / Finance / Failed QC / Restore flows.
        """
        condition = str(condition or "").strip().upper()
        grouped = {}

        for row in rows:
            row_items = normalized_by_usage.get(int(row.pk), [])
            matching = [
                item
                for item in row_items
                if str(item.get("condition") or "").strip().upper()
                == condition
            ]

            if not matching:
                continue

            component = getattr(row, "component", None)
            component_id = getattr(row, "component_id", None)
            component_code = str(
                getattr(component, "component_id", "") or ""
            ).strip()
            component_name = str(
                getattr(row, "component_name", "")
                or getattr(component, "name", "")
                or "Component"
            ).strip()
            label = " - ".join(
                value
                for value in [component_code, component_name]
                if value
            ) or component_name

            key = str(component_id or component_code or component_name)
            entry = grouped.setdefault(
                key,
                {
                    "component": component_id,
                    "component_code": component_code,
                    "component_name": component_name,
                    "label": label,
                    "serial_numbers": [],
                    "quantity": 0,
                    "remarks": [],
                    "qc_items": [],
                },
            )

            for item in matching:
                serial = str(item.get("serial_number") or "").strip()
                remark = str(item.get("remarks") or "").strip()
                unit_index = int(item.get("unit_index") or 0)

                if serial and serial not in entry["serial_numbers"]:
                    entry["serial_numbers"].append(serial)

                if remark and remark not in entry["remarks"]:
                    entry["remarks"].append(remark)

                entry["qc_items"].append(
                    {
                        "serial_number": serial,
                        "unit_index": unit_index,
                        "condition": condition,
                        "remarks": remark,
                    }
                )

                entry["quantity"] += 1

        return list(grouped.values())

    @classmethod
    def _find_returnable_qc_scrap(
        cls,
        *,
        material_request,
        movement_id,
    ):
        if material_request is None:
            return None

        candidates = (
            OutwardEntry.objects
            .select_for_update()
            .filter(
                outward_type="SCRAP",
                material_request=material_request,
            )
            .order_by("-id")
        )

        for candidate in candidates:
            metadata = (
                candidate.inventory_allocations
                if isinstance(candidate.inventory_allocations, dict)
                else {}
            )
            workflow = str(metadata.get("workflow") or "").strip().upper()

            if workflow not in {
                "RETURNABLE_COMPONENT_QC_V1",
                "RETURNABLE_DRONE_QC_V1",
            }:
                continue

            existing_movement = str(
                metadata.get("returnable_movement_id") or ""
            ).strip()

            if movement_id and existing_movement == movement_id:
                return candidate

        return None

    @classmethod
    def _create_returnable_qc_scrap(
        cls,
        *,
        request,
        rows,
        normalized_by_usage,
        drone_mode,
    ):
        """
        Convert a Returnable QC failure into the EXISTING Scrap approval flow:

            Inventory QC Failed
                -> Manager Notifications > Scrap
                -> Manager Restore/Rebuild YES or NO
                -> Finance Notifications > Scrap
                -> Finance Approve/Reject
                -> optional Procurement Restore (component Restore=YES)

        No second stock deduction happens here. This row is an approval /
        disposition record for already-issued-and-returned physical units.
        """
        if not rows:
            return None

        first = rows[0]
        material_request = first.material_request
        mr_number = (
            getattr(material_request, "material_request_id", "")
            or "Returnable"
        )
        purpose = str(first.purpose or "").strip().upper()
        purpose_label = cls._returnable_purpose_label(purpose)

        movement_id = cls._movement_id_for_usage(first)
        if not movement_id:
            movement_id = (
                f"LEGACY-RETURN-{material_request.pk if material_request else 'MR'}-"
                f"{purpose}-{first.requested_date}-{first.received_date}"
            )

        existing = cls._find_returnable_qc_scrap(
            material_request=material_request,
            movement_id=movement_id,
        )
        if existing is not None:
            return existing

        bad_items = cls._returnable_qc_component_items(
            rows,
            normalized_by_usage,
            condition="NOT_OK",
        )
        good_items = cls._returnable_qc_component_items(
            rows,
            normalized_by_usage,
            condition="OK",
        )

        if not bad_items:
            return None

        bad_quantity = sum(
            max(int(item.get("quantity") or 0), 0)
            for item in bad_items
        )
        bad_serials = cls.normalize_serials(
            [
                serial
                for item in bad_items
                for serial in (item.get("serial_numbers") or [])
            ]
        )

        # Every NOT OK remark is preserved in the real Scrap Remarks.
        # This is the text Manager and Finance see in their Scrap tab.
        remark_parts = []
        for item in bad_items:
            component_name = (
                item.get("label")
                or item.get("component_name")
                or "Component"
            )
            for qc_item in item.get("qc_items") or []:
                unit_ref = (
                    qc_item.get("serial_number")
                    or (
                        f"Unit {qc_item.get('unit_index')}"
                        if qc_item.get("unit_index")
                        else "Unit"
                    )
                )
                qc_remark = str(qc_item.get("remarks") or "").strip()
                if qc_remark:
                    remark_parts.append(
                        f"{component_name} [{unit_ref}]: {qc_remark}"
                    )

        qc_remarks = " | ".join(remark_parts)
        outward_remarks = (
            f"Returnable {purpose_label} QC Failed - {mr_number}."
            + (
                f" NOT OK Remarks: {qc_remarks}"
                if qc_remarks
                else " One or more returned components were marked NOT OK."
            )
        )

        workflow = (
            "RETURNABLE_DRONE_QC_V1"
            if drone_mode
            else "RETURNABLE_COMPONENT_QC_V1"
        )

        metadata = {
            "workflow": workflow,
            "scrap_mode": "PARTIAL",
            "reorder_choice": "PENDING_MANAGER",
            "returnable_movement_id": movement_id,
            "returnable_usage_ids": [int(row.pk) for row in rows],
            "returnable_purpose": purpose,
            "returnable_purpose_label": purpose_label,
            "source_mr_id": getattr(material_request, "pk", None),
            "source_mr_number": mr_number,
            "source_mr_request_type": str(
                getattr(material_request, "request_type", "") or ""
            ),
            "source_mr_customized_bom": bool(
                getattr(material_request, "customized_bom", False)
            ),
            # BAD = actual Scrap.
            "scrap_items": bad_items,
            # GOOD = reusable pieces of a failed returned drone.
            # For loose component QC this remains empty because only failed
            # units enter the Scrap/Restore decision.
            "selected_items": good_items if drone_mode else [],
            "return_items": [],
            "reorder_items": [],
            "good_items": good_items,
            "failed_items": bad_items,
            "scrap_quantity": bad_quantity,
            "selected_quantity": sum(
                int(item.get("quantity") or 0)
                for item in (good_items if drone_mode else [])
            ),
            "return_quantity": 0,
            "reorder_quantity": 0,
            "qc_failure_remarks": remark_parts,
            "disposition_processed": False,
            "manager_disposition_decision": "",
            "manager_decided_by": "",
            "manager_decided_at": "",
            "replacement_mr_id": None,
            "replacement_mr_number": "",
            "returned_inventory_ids": [],
            "procurement_restore_ready": False,
            "procurement_restore_status": "WAITING_MANAGER_FINANCE",
        }

        single_component_id = None
        if len(bad_items) == 1:
            single_component_id = bad_items[0].get("component")

        product_name = (
            bad_items[0].get("label")
            if len(bad_items) == 1
            else f"{bad_quantity} Returnable QC Failed item(s) - {mr_number}"
        )

        actor_name = cls._actor_name(request.user)
        stamp = timezone.now().strftime("%Y%m%d%H%M%S%f")
        code = f"OUT-{stamp}-{uuid4().hex[:6].upper()}"

        scrap_entry = OutwardEntry.objects.create(
            code=code,
            outward_type="SCRAP",
            item_type="COMPONENT",
            out_date=timezone.localdate(),
            product_name=product_name,
            component_id=single_component_id,
            quantity=max(bad_quantity, 1),
            no_of_components=max(bad_quantity, 1),
            serial_numbers=bad_serials,
            inventory_allocations=metadata,
            stock_deducted=False,
            stock_restored=False,
            material_request=material_request,
            source="ENGINEER",
            scrap_origin="MR",
            requested_by=actor_name,
            requested_by_user_id=getattr(request.user, "pk", None),
            moved_to_inventory=False,
            moved_at=None,
            remarks=outward_remarks,
            approval_status="PENDING_MANAGER",
            status="PENDING_MANAGER",
        )

        Notification.objects.update_or_create(
            category="SCRAP",
            receiver="MANAGER",
            reference_id=str(scrap_entry.pk),
            defaults={
                "requested_by": actor_name,
                "title": f"Returnable QC Failed - {mr_number}",
                "message": outward_remarks,
                "status": "PENDING_MANAGER",
                "is_read": False,
            },
        )

        # Link the official Scrap row back to every Returnable audit row.
        for row in rows:
            cls._set_usage_metadata(
                row,
                return_qc_scrap_id=scrap_entry.pk,
                return_qc_scrap_code=scrap_entry.code,
                return_qc_scrap_workflow=workflow,
            )
            row.save(update_fields=["inventory_issue_details"])

        return scrap_entry

    @action(
        detail=True,
        methods=["post"],
        url_path="return-qc",
    )
    @transaction.atomic
    def return_qc(self, request, pk=None):
        """
        Inventory serial-level QC for a returned movement.

        qc_items must contain every returned unit/serial with condition OK or
        NOT_OK. NOT_OK requires remarks. Engineer never calls this endpoint.
        Returned rows stay in ComponentUsage permanently for audit history.
        """
        try:
            self.require_role(request, {"inventory", "admin"})
        except PermissionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )

        usage = (
            ComponentUsage.objects
            .select_for_update()
            .select_related("component", "material_request")
            .filter(pk=pk)
            .first()
        )
        if usage is None:
            return Response(
                {"detail": "Returned usage record was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        rows = [
            row
            for row in self._movement_rows(usage, lock=True)
            if row.received_date
        ]
        self._hydrate_missing_usage_serials(
            rows
        )
        if not rows:
            return Response(
                {"detail": "This movement has not been moved to Returned yet."},
                status=status.HTTP_409_CONFLICT,
            )

        qc_items = request.data.get("qc_items")
        if not isinstance(qc_items, list) or not qc_items:
            return Response(
                {"qc_items": "Serial-level QC items are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        row_by_id = {int(row.pk): row for row in rows}
        normalized_by_usage = {int(row.pk): [] for row in rows}

        for item in qc_items:
            if not isinstance(item, dict):
                return Response(
                    {"qc_items": "Each QC item must be an object."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                usage_id = int(item.get("usage_id"))
            except (TypeError, ValueError):
                usage_id = 0
            if usage_id not in row_by_id:
                return Response(
                    {"qc_items": "QC item does not belong to this returned movement."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            condition = str(item.get("condition") or "").strip().upper()
            if condition not in {"OK", "NOT_OK"}:
                return Response(
                    {"qc_items": "Every returned serial/component must be marked OK or NOT OK."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            remarks = str(item.get("remarks") or "").strip()
            if condition == "NOT_OK" and not remarks:
                return Response(
                    {"qc_items": "Remarks are mandatory for every NOT OK serial/component."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            normalized_by_usage[usage_id].append(
                {
                    "usage_id": usage_id,
                    "component_id": item.get("component_id"),
                    "serial_number": str(item.get("serial_number") or "").strip(),
                    "unit_index": int(item.get("unit_index") or 0),
                    "condition": condition,
                    "remarks": remarks,
                }
            )

        # Require one QC result for every returned unit.
        for row in rows:
            expected_serials = self.normalize_serials(row.issued_serial_numbers)
            expected_count = len(expected_serials) or max(int(row.quantity or 0), 1)
            actual = normalized_by_usage.get(int(row.pk), [])
            if len(actual) != expected_count:
                return Response(
                    {
                        "qc_items": (
                            f"{row.component_name or 'Component'} requires "
                            f"{expected_count} QC result(s); received {len(actual)}."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if expected_serials:
                actual_serials = sorted(
                    str(item.get("serial_number") or "").strip()
                    for item in actual
                )
                if actual_serials != sorted(expected_serials):
                    return Response(
                        {"qc_items": f"Serial numbers do not match the issued serials for {row.component_name or 'Component'}."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        request_type = str(
            getattr(usage.material_request, "request_type", "") or ""
        ).strip().upper()
        purpose = str(usage.purpose or "").strip().upper()
        drone_mode = (
            request_type != "RETURNABLE"
            and purpose in {"FLIGHT_TEST", "CUSTOMER_DEMO", "EVENT"}
        )
        any_bad_in_movement = any(
            item["condition"] == "NOT_OK"
            for items in normalized_by_usage.values()
            for item in items
        )

        for row in rows:
            row_items = normalized_by_usage[int(row.pk)]
            row_has_bad = any(
                item["condition"] == "NOT_OK"
                for item in row_items
            )
            effective_bad = any_bad_in_movement if drone_mode else row_has_bad

            self._set_usage_metadata(
                row,
                return_qc_status=("FAILED" if effective_bad else "PASSED"),
                return_qc_items=row_items,
                qc_checked_by=self._actor_name(request.user),
                qc_checked_at=timezone.now().isoformat(),
                good_serials=[
                    item["serial_number"]
                    for item in row_items
                    if item["condition"] == "OK" and item["serial_number"]
                ],
                bad_serials=[
                    item["serial_number"]
                    for item in row_items
                    if item["condition"] == "NOT_OK" and item["serial_number"]
                ],
            )

            if effective_bad:
                row.return_condition = "NOT_OK"
                bad_reasons = [
                    item["remarks"]
                    for item in row_items
                    if item["condition"] == "NOT_OK" and item["remarks"]
                ]
                row.return_reason = "; ".join(bad_reasons) or "Returned QC failed."
                row.return_approval_status = "PENDING_MANAGER"
            else:
                row.return_condition = "OK"
                row.return_reason = ""
                row.return_approval_status = "COMPLETED"

            row.save(
                update_fields=[
                    "return_condition",
                    "return_reason",
                    "return_approval_status",
                    "inventory_issue_details",
                ]
            )

        first = rows[0]
        mr_number = (
            getattr(first.material_request, "material_request_id", "")
            or "Returnable"
        )

        Notification.objects.filter(
            category="CU",
            receiver="INVENTORY",
            reference_id=str(first.pk),
        ).update(
            status="QC_CHECKED",
            is_read=True,
        )

        if any_bad_in_movement:
            scrap_entry = self._create_returnable_qc_scrap(
                request=request,
                rows=rows,
                normalized_by_usage=normalized_by_usage,
                drone_mode=drone_mode,
            )

            qc_status = "QC_FAILED_PENDING_MANAGER"
        else:
            scrap_entry = None
            qc_status = (
                "QC_PASSED_DRONE_READY"
                if drone_mode
                else "QC_PASSED_RETURNED_TO_STORE"
            )

        return Response(
            {
                "detail": (
                    "Return QC failed and was sent to Manager."
                    if any_bad_in_movement
                    else "Return QC passed."
                ),
                "qc_status": qc_status,
                "material_request_id": mr_number,
                "scrap_id": (
                    scrap_entry.pk
                    if scrap_entry is not None
                    else None
                ),
                "scrap_code": (
                    scrap_entry.code
                    if scrap_entry is not None
                    else ""
                ),
                "rows": self.get_serializer(rows, many=True).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="return-decision")
    @transaction.atomic
    def return_decision(self, request, pk=None):
        try:
            self.require_role(request, {"inventory", "admin"})
        except PermissionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        usage = (
            ComponentUsage.objects
            .select_for_update()
            .select_related("component", "material_request")
            .get(pk=pk)
        )

        condition = str(request.data.get("condition", "") or "").strip().upper()
        reason = str(request.data.get("reason", "") or "").strip()

        if condition not in {"OK", "NOT_OK"}:
            return Response(
                {"detail": "condition must be OK or NOT_OK."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if condition == "OK":
            if usage.inventory_adjusted and not usage.inventory_returned:
                try:
                    self.restore_usage_stock(usage)
                    usage.inventory_returned = True
                except ValueError as exc:
                    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            usage.received_date = timezone.localdate()
            usage.return_condition = "OK"
            usage.return_reason = ""
            usage.return_approval_status = "COMPLETED"
            usage.save(
                update_fields=[
                    "received_date",
                    "return_condition",
                    "return_reason",
                    "return_approval_status",
                    "inventory_returned",
                ]
            )
            Notification.objects.filter(category="CU", reference_id=str(usage.pk)).update(
                status="APPROVED",
                is_read=True,
            )
        else:
            if not reason:
                return Response(
                    {"detail": "Reason is mandatory when the returned item is Not OK."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            usage.received_date = timezone.localdate()
            usage.return_condition = "NOT_OK"
            usage.return_reason = reason
            usage.return_approval_status = "PENDING_MANAGER"
            usage.save(
                update_fields=[
                    "received_date",
                    "return_condition",
                    "return_reason",
                    "return_approval_status",
                ]
            )

            mr_number = getattr(usage.material_request, "material_request_id", "") or "Returnable"
            self.upsert_return_notification(
                usage,
                receiver="MANAGER",
                status_value="PENDING_MANAGER",
                title=f"Returnable Not OK - {mr_number}",
                message=(
                    f"{usage.component_name} was returned as Not OK. "
                    "Manager approval is required before Finance review."
                ),
            )

        output = self.get_serializer(usage)
        return Response(output.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="return-approval")
    @transaction.atomic
    def return_approval(self, request, pk=None):
        usage = (
            ComponentUsage.objects
            .select_for_update()
            .select_related("component", "material_request")
            .get(pk=pk)
        )

        decision = str(request.data.get("decision", "") or "").strip().upper()
        reason = str(request.data.get("reason", "") or "").strip()
        role = self.get_active_role(request)

        if decision not in {"APPROVE", "REJECT"}:
            return Response(
                {"detail": "decision must be APPROVE or REJECT."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if role == "manager":
            if usage.return_approval_status != "PENDING_MANAGER":
                return Response(
                    {"detail": "This return is not waiting for Manager approval."},
                    status=status.HTTP_409_CONFLICT,
                )

            Notification.objects.filter(
                category="CU",
                reference_id=str(usage.pk),
                receiver="MANAGER",
            ).update(
                status="MANAGER_APPROVED" if decision == "APPROVE" else "MANAGER_REJECTED",
                is_read=True,
            )

            if decision == "REJECT":
                usage.return_approval_status = "REJECTED"
                if reason:
                    usage.return_reason = f"{usage.return_reason}\nManager: {reason}".strip()
            else:
                usage.return_approval_status = "PENDING_FINANCE"
                mr_number = getattr(usage.material_request, "material_request_id", "") or "Returnable"
                self.upsert_return_notification(
                    usage,
                    receiver="FINANCE",
                    status_value="PENDING_FINANCE",
                    title=f"Returnable Not OK - Finance Review - {mr_number}",
                    message=(
                        f"Manager approved the Not OK return for {usage.component_name}. "
                        "Finance approval is required."
                    ),
                )

        elif role == "finance":
            if usage.return_approval_status != "PENDING_FINANCE":
                return Response(
                    {"detail": "This return is not waiting for Finance approval."},
                    status=status.HTTP_409_CONFLICT,
                )

            Notification.objects.filter(
                category="CU",
                reference_id=str(usage.pk),
                receiver="FINANCE",
            ).update(
                status="FINANCE_APPROVED" if decision == "APPROVE" else "FINANCE_REJECTED",
                is_read=True,
            )

            usage.return_approval_status = (
                "APPROVED" if decision == "APPROVE" else "REJECTED"
            )
            if decision == "REJECT" and reason:
                usage.return_reason = f"{usage.return_reason}\nFinance: {reason}".strip()
        else:
            return Response(
                {"detail": "Only Manager or Finance can process a Not OK return approval."},
                status=status.HTTP_403_FORBIDDEN,
            )

        usage.save(update_fields=["return_approval_status", "return_reason"])
        return Response(self.get_serializer(usage).data, status=status.HTTP_200_OK)

    # --------------------------------------------------------------
    # INVENTORY COMPONENT DROPDOWN API
    # --------------------------------------------------------------
    @action(
        detail=False,
        methods=["get"],
        url_path="inventory-options",
    )
    def inventory_options(self, request):
        component_ids = (
            Inventory.objects
            .filter(
                issued=False,
                quantity__gt=0,
            )
            .values_list(
                "component_id",
                flat=True,
            )
            .distinct()
        )

        components = (
            Component.objects
            .filter(
                id__in=component_ids,
                is_active=True,
            )
            .order_by(
                "category",
                "name",
            )
        )

        rows = []

        for component in components:
            snapshot = (
                self.build_usage_stock_snapshot(
                    component.id
                )
            )

            if snapshot["available_quantity"] <= 0:
                continue

            rows.append(
                {
                    "id": component.id,
                    "component_id": (
                        component.component_id
                    ),
                    "name": component.name,
                    "category": component.category,
                    "available_quantity": (
                        snapshot[
                            "available_quantity"
                        ]
                    ),
                    "physical_quantity": (
                        snapshot[
                            "physical_quantity"
                        ]
                    ),
                    "reserved_quantity": (
                        snapshot[
                            "reserved_quantity"
                        ]
                    ),
                    "serial_numbers": (
                        snapshot[
                            "available_serials"
                        ]
                    ),
                }
            )

        return Response(rows)

    # --------------------------------------------------------------
    # CREATE / UPDATE / DELETE
    # --------------------------------------------------------------
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(
            data=request.data
        )
        serializer.is_valid(
            raise_exception=True
        )

        usage = serializer.save()

        selected_serials = getattr(
            usage,
            "_selected_serials_for_issue",
            [],
        )

        try:
            if (
                usage.item_source == "INVENTORY"
                and usage.issued_date
            ):
                (
                    issue_details,
                    issued_serials,
                ) = self.deduct_usage_stock(
                    usage.component_id,
                    usage.quantity,
                    selected_serials=(
                        selected_serials
                    ),
                )

                usage.inventory_issue_details = (
                    issue_details
                )
                usage.issued_serial_numbers = (
                    issued_serials
                )
                usage.inventory_adjusted = True
                usage.inventory_returned = False

                usage.save(
                    update_fields=[
                        "inventory_issue_details",
                        "issued_serial_numbers",
                        "inventory_adjusted",
                        "inventory_returned",
                    ]
                )

                # Creating directly as RETURNED is supported safely:
                # deduct first, then restore in the same transaction.
                if usage.received_date:
                    self.restore_usage_stock(usage)

                    usage.inventory_returned = True
                    usage.save(
                        update_fields=[
                            "inventory_returned"
                        ]
                    )

        except ValueError as exc:
            transaction.set_rollback(True)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        output = self.get_serializer(usage)

        return Response(
            output.data,
            status=status.HTTP_201_CREATED,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        partial = kwargs.pop(
            "partial",
            False,
        )

        usage = (
            ComponentUsage.objects
            .select_for_update()
            .select_related("component")
            .get(pk=kwargs.get("pk"))
        )

        old_item_source = usage.item_source
        old_component_id = usage.component_id
        old_quantity = usage.quantity
        old_issued_date = usage.issued_date
        old_received_date = usage.received_date
        old_inventory_adjusted = (
            usage.inventory_adjusted
        )
        old_inventory_returned = (
            usage.inventory_returned
        )

        serializer = self.get_serializer(
            usage,
            data=request.data,
            partial=partial,
        )
        serializer.is_valid(
            raise_exception=True
        )

        # Once stock is physically issued, do not allow edits that could
        # make the audit trail disagree with Inventory.
        if (
            old_item_source == "INVENTORY"
            and old_inventory_adjusted
            and not old_inventory_returned
        ):
            proposed_component = (
                serializer.validated_data.get(
                    "component",
                    usage.component,
                )
            )
            proposed_quantity = int(
                serializer.validated_data.get(
                    "quantity",
                    old_quantity,
                )
                or 0
            )
            proposed_source = str(
                serializer.validated_data.get(
                    "item_source",
                    old_item_source,
                )
                or ""
            ).upper()

            if (
                proposed_component is None
                or proposed_component.id
                != old_component_id
                or proposed_quantity
                != int(old_quantity or 0)
                or proposed_source
                != "INVENTORY"
            ):
                return Response(
                    {
                        "detail": (
                            "Component, source and quantity "
                            "cannot be changed after In-Store "
                            "stock has been issued. Return the "
                            "item first."
                        )
                    },
                    status=(
                        status.HTTP_400_BAD_REQUEST
                    ),
                )

        usage = serializer.save()

        selected_serials = getattr(
            usage,
            "_selected_serials_for_issue",
            [],
        )

        try:
            # PENDING -> ISSUED
            should_issue_inventory = (
                usage.item_source == "INVENTORY"
                and usage.issued_date
                and not usage.inventory_adjusted
            )

            if should_issue_inventory:
                (
                    issue_details,
                    issued_serials,
                ) = self.deduct_usage_stock(
                    usage.component_id,
                    usage.quantity,
                    selected_serials=(
                        selected_serials
                    ),
                )

                usage.inventory_issue_details = (
                    issue_details
                )
                usage.issued_serial_numbers = (
                    issued_serials
                )
                usage.inventory_adjusted = True
                usage.inventory_returned = False

                usage.save(
                    update_fields=[
                        "inventory_issue_details",
                        "issued_serial_numbers",
                        "inventory_adjusted",
                        "inventory_returned",
                    ]
                )

            # ISSUED -> RETURNED
            should_return_inventory = (
                usage.item_source == "INVENTORY"
                and usage.received_date
                and usage.inventory_adjusted
                and not usage.inventory_returned
            )

            if should_return_inventory:
                self.restore_usage_stock(
                    usage
                )

                usage.inventory_returned = True
                usage.save(
                    update_fields=[
                        "inventory_returned"
                    ]
                )

        except ValueError as exc:
            transaction.set_rollback(True)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        output = self.get_serializer(usage)
        return Response(output.data)

    def partial_update(
        self,
        request,
        *args,
        **kwargs,
    ):
        kwargs["partial"] = True
        return self.update(
            request,
            *args,
            **kwargs,
        )

    @transaction.atomic
    def destroy(
        self,
        request,
        *args,
        **kwargs,
    ):
        usage = (
            ComponentUsage.objects
            .select_for_update()
            .get(pk=kwargs.get("pk"))
        )

        if (
            usage.item_source == "INVENTORY"
            and usage.inventory_adjusted
            and not usage.inventory_returned
        ):
            return Response(
                {
                    "detail": (
                        "This In-Store item is still issued. "
                        "Mark it Returned before deleting the "
                        "usage record so Inventory remains correct."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        usage.delete()
        return Response(
            status=status.HTTP_204_NO_CONTENT
        )