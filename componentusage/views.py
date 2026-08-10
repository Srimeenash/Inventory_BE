from django.db import transaction
from django.db.models import Sum

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from components.models import Component
from inventory.models import (
    Inventory,
    InventoryReservation,
)

from .models import ComponentUsage
from .serializers import ComponentUsageSerializer


class ComponentUsageViewSet(ModelViewSet):
    queryset = (
        ComponentUsage.objects
        .select_related("component")
        .all()
    )
    serializer_class = ComponentUsageSerializer
    pagination_class = None

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