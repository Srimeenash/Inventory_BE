from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from inventory.models import Inventory, InventoryReservation

from .models import OutwardEntry
from .serializers import OutwardEntrySerializer


class OutwardEntryViewSet(viewsets.ModelViewSet):
    """
    Direct Sales/Event Outward workflow.

    SALES + COMPONENT:
        Deducts central In-Store quantity and exact serials permanently.

    EVENT + COMPONENT:
        Deducts central In-Store quantity and exact serials temporarily.
        Returned-good quantity is restored through PATCH.

    SALES/EVENT + DRONE:
        Stores only the manually entered drone name. It has no MR link and
        does not change component Inventory.
    """

    queryset = (
        OutwardEntry.objects
        .select_related("component")
        .all()
        .order_by("-out_date", "-created_at", "-id")
    )
    serializer_class = OutwardEntrySerializer
    pagination_class = None

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
    def ensure_inventory_serials(cls, stock_row):
        serials = cls.normalize_serials(stock_row.serial_numbers)
        quantity = max(int(stock_row.quantity or 0), 0)

        prefix = "".join(
            character
            for character in str(
                stock_row.inventory_code or f"INV{stock_row.pk}"
            )
            if character.isalnum()
        ).upper() or f"INV{stock_row.pk}"

        used = set(serials) | set(
            cls.normalize_serials(stock_row.issued_serial_numbers)
        )
        index = 1

        while len(serials) < quantity:
            serial = f"CINV_{prefix}_S{index:05d}"
            index += 1
            if serial in used:
                continue
            used.add(serial)
            serials.append(serial)

        serials = serials[:quantity]

        if serials != cls.normalize_serials(stock_row.serial_numbers):
            stock_row.serial_numbers = serials
            stock_row.save(update_fields=["serial_numbers"])

        return serials

    @classmethod
    def deduct_component_stock(
        cls,
        *,
        component_id,
        quantity,
        selected_serials=None,
    ):
        requested = max(int(quantity or 0), 0)
        selected = cls.normalize_serials(selected_serials)

        if requested <= 0:
            raise ValidationError(
                {"quantity": "Quantity must be greater than zero."}
            )

        if selected and len(selected) != requested:
            raise ValidationError(
                {
                    "serial_numbers": (
                        "Selected serial count must equal the requested quantity."
                    )
                }
            )

        stock_rows = list(
            Inventory.objects
            .select_for_update()
            .select_related("component")
            .filter(
                component_id=component_id,
                issued=False,
                quantity__gt=0,
            )
            .order_by("received_date", "created_at", "id")
        )

        # Manager-approved MR reservations remain physically in Inventory
        # until issued, but Sales/Event must never consume that protected stock.
        reservation_rows = list(
            InventoryReservation.objects
            .select_for_update()
            .filter(component_id=component_id)
            .exclude(status__in=["RELEASED", "CANCELLED", "ISSUED"])
        )
        reserved_quantity = sum(
            max(
                int(row.reserved_store_quantity or 0)
                - int(row.issued_store_quantity or 0),
                0,
            )
            for row in reservation_rows
        )
        physical_quantity = sum(
            max(int(row.quantity or 0), 0)
            for row in stock_rows
        )
        free_quantity = max(
            physical_quantity - reserved_quantity,
            0,
        )

        if requested > free_quantity:
            raise ValidationError(
                {
                    "quantity": (
                        f"Only {free_quantity} unreserved item(s) are available "
                        "in In Store. The remaining stock is reserved for "
                        "Material Requests."
                    )
                }
            )

        available_by_serial = {}
        available_in_order = []

        for stock_row in stock_rows:
            for serial in cls.ensure_inventory_serials(stock_row):
                if serial not in available_by_serial:
                    available_by_serial[serial] = stock_row
                    available_in_order.append(serial)

        chosen = selected or available_in_order[:requested]

        missing = [
            serial
            for serial in chosen
            if serial not in available_by_serial
        ]
        if missing:
            raise ValidationError(
                {
                    "serial_numbers": (
                        "One or more serials are no longer available: "
                        + ", ".join(missing)
                    )
                }
            )

        if len(chosen) < requested:
            raise ValidationError(
                {
                    "quantity": (
                        f"Only {len(chosen)} item(s) are available in In Store; "
                        f"{requested} were requested."
                    )
                }
            )

        chosen_set = set(chosen)
        allocations = []
        actually_deducted = []

        for stock_row in stock_rows:
            current_serials = cls.ensure_inventory_serials(stock_row)
            row_serials = [
                serial
                for serial in current_serials
                if serial in chosen_set
            ]

            if not row_serials:
                continue

            row_serial_set = set(row_serials)
            remaining_serials = [
                serial
                for serial in current_serials
                if serial not in row_serial_set
            ]

            stock_row.quantity = len(remaining_serials)
            stock_row.serial_numbers = remaining_serials
            stock_row.issued_serial_numbers = cls.normalize_serials(
                cls.normalize_serials(stock_row.issued_serial_numbers)
                + row_serials
            )
            stock_row.issued = stock_row.quantity == 0
            stock_row.save(
                update_fields=[
                    "quantity",
                    "serial_numbers",
                    "issued_serial_numbers",
                    "issued",
                ]
            )

            allocations.append(
                {
                    "inventory_id": stock_row.id,
                    "inventory_code": stock_row.inventory_code,
                    "quantity": len(row_serials),
                    "serial_numbers": row_serials,
                }
            )
            actually_deducted.extend(row_serials)

        ordered_deducted = [
            serial
            for serial in chosen
            if serial in set(actually_deducted)
        ]

        if len(ordered_deducted) != requested:
            raise ValidationError(
                {
                    "quantity": (
                        "In-Store deduction was incomplete. Nothing was saved."
                    )
                }
            )

        return ordered_deducted, allocations

    @staticmethod
    def generate_return_inventory_code():
        stamp = timezone.now().strftime("%Y%m%d%H%M%S%f")
        return f"EVR-{stamp}-{uuid4().hex[:5].upper()}"

    @classmethod
    def get_return_stock_row(cls, outward, allocation):
        inventory_id = allocation.get("inventory_id")

        if inventory_id:
            stock_row = (
                Inventory.objects
                .select_for_update()
                .filter(pk=inventory_id)
                .first()
            )
            if stock_row is not None:
                return stock_row

        # Defensive fallback if an original Inventory row was deleted.
        return Inventory.objects.create(
            inventory_code=cls.generate_return_inventory_code(),
            component=outward.component,
            category=(
                getattr(outward.component, "category", "") or ""
            ),
            vendor="EVENT RETURN",
            purchase_order=outward.code,
            quantity=0,
            received_date=timezone.localdate(),
            total_price=0,
            issued=False,
            serial_numbers=[],
            issued_serial_numbers=[],
        )

    @classmethod
    def restore_event_component_stock(
        cls,
        outward,
        target_returned_quantity,
    ):
        total_quantity = max(int(outward.quantity or 0), 0)
        current_returned = max(
            int(outward.returned_quantity or 0),
            0,
        )
        target = max(int(target_returned_quantity or 0), 0)

        if target < current_returned:
            raise ValidationError(
                {
                    "returned_quantity": (
                        "Returned quantity cannot be reduced after stock has "
                        "already been restored."
                    )
                }
            )

        if target > total_quantity:
            raise ValidationError(
                {
                    "returned_quantity": (
                        f"Returned quantity cannot exceed Event quantity "
                        f"({total_quantity})."
                    )
                }
            )

        restore_count = target - current_returned
        existing_returned = cls.normalize_serials(
            outward.returned_serial_numbers
        )

        if restore_count == 0:
            return existing_returned

        issued_serials = cls.normalize_serials(outward.serial_numbers)
        available_to_restore = [
            serial
            for serial in issued_serials
            if serial not in set(existing_returned)
        ]
        serials_to_restore = available_to_restore[:restore_count]

        if len(serials_to_restore) != restore_count:
            raise ValidationError(
                {
                    "returned_quantity": (
                        "The Event row does not contain enough unreturned "
                        "serials to restore this quantity."
                    )
                }
            )

        allocations = (
            outward.inventory_allocations
            if isinstance(outward.inventory_allocations, list)
            else []
        )
        remaining = set(serials_to_restore)

        for allocation in allocations:
            if not isinstance(allocation, dict):
                continue

            allocation_serials = cls.normalize_serials(
                allocation.get("serial_numbers")
            )
            restore_for_row = [
                serial
                for serial in allocation_serials
                if serial in remaining
            ]

            if not restore_for_row:
                continue

            stock_row = cls.get_return_stock_row(
                outward,
                allocation,
            )
            current_serials = cls.ensure_inventory_serials(stock_row)
            current_issued = cls.normalize_serials(
                stock_row.issued_serial_numbers
            )

            for serial in restore_for_row:
                if serial not in current_serials:
                    current_serials.append(serial)

            restore_set = set(restore_for_row)
            current_issued = [
                serial
                for serial in current_issued
                if serial not in restore_set
            ]

            stock_row.serial_numbers = current_serials
            stock_row.quantity = len(current_serials)
            stock_row.issued_serial_numbers = current_issued
            stock_row.issued = False
            stock_row.save(
                update_fields=[
                    "serial_numbers",
                    "quantity",
                    "issued_serial_numbers",
                    "issued",
                ]
            )

            remaining.difference_update(restore_set)

        if remaining:
            # Old rows may not contain allocation metadata. Restore those
            # serials into one controlled EVENT RETURN Inventory row.
            fallback_row = cls.get_return_stock_row(
                outward,
                {},
            )
            current_serials = cls.ensure_inventory_serials(fallback_row)
            current_issued = cls.normalize_serials(
                fallback_row.issued_serial_numbers
            )

            for serial in serials_to_restore:
                if serial in remaining and serial not in current_serials:
                    current_serials.append(serial)

            current_issued = [
                serial
                for serial in current_issued
                if serial not in remaining
            ]

            fallback_row.serial_numbers = current_serials
            fallback_row.quantity = len(current_serials)
            fallback_row.issued_serial_numbers = current_issued
            fallback_row.issued = False
            fallback_row.save(
                update_fields=[
                    "serial_numbers",
                    "quantity",
                    "issued_serial_numbers",
                    "issued",
                ]
            )

        return cls.normalize_serials(
            existing_returned + serials_to_restore
        )

    def save_stock_aware_entry(self, serializer):
        validated = serializer.validated_data
        outward_type = str(
            validated.get("outward_type") or "SCRAP"
        ).strip().upper()
        item_type = str(
            validated.get("item_type") or "COMPONENT"
        ).strip().upper()
        quantity = max(
            int(
                validated.get("quantity")
                or validated.get("no_of_components")
                or 1
            ),
            1,
        )

        save_values = {
            "approval_status": "NOT_REQUESTED",
            "quantity": quantity,
            "no_of_components": quantity,
            "returned_quantity": 0,
            "returned_serial_numbers": [],
            "stock_restored": False,
        }

        component = validated.get("component")

        if (
            outward_type in {"SALES", "EVENT"}
            and item_type == "COMPONENT"
        ):
            selected_serials = self.normalize_serials(
                validated.get("serial_numbers")
            )
            serials, allocations = self.deduct_component_stock(
                component_id=component.id,
                quantity=quantity,
                selected_serials=selected_serials,
            )

            component_code = str(
                getattr(component, "component_id", "") or ""
            ).strip()
            component_name = str(
                getattr(component, "name", "") or ""
            ).strip()
            component_label = " - ".join(
                value
                for value in [component_code, component_name]
                if value
            )

            save_values.update(
                {
                    "product_name": (
                        validated.get("product_name")
                        or component_label
                        or component_name
                        or component_code
                    ),
                    "serial_numbers": serials,
                    "inventory_allocations": allocations,
                    "stock_deducted": True,
                    "status": (
                        "SOLD"
                        if outward_type == "SALES"
                        else "EVENT_OUT"
                    ),
                }
            )
        else:
            product_name = str(
                validated.get("product_name")
                or validated.get("drone_name")
                or ""
            ).strip()
            save_values.update(
                {
                    "product_name": product_name,
                    "drone_name": (
                        product_name
                        if item_type == "DRONE"
                        else validated.get("drone_name")
                    ),
                    "serial_numbers": [],
                    "inventory_allocations": [],
                    "stock_deducted": False,
                    "status": (
                        "SOLD"
                        if outward_type == "SALES"
                        else "EVENT_OUT"
                        if outward_type == "EVENT"
                        else validated.get("status") or "NEW"
                    ),
                }
            )

        return serializer.save(**save_values)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            instance = self.save_stock_aware_entry(serializer)

        output = self.get_serializer(instance)
        headers = self.get_success_headers(output.data)
        return Response(
            output.data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="bulk-create",
    )
    def bulk_create(self, request):
        items = request.data.get("items")

        if not isinstance(items, list) or not items:
            raise ValidationError(
                {"items": "Add at least one Component or Drone."}
            )

        common = {
            key: value
            for key, value in request.data.items()
            if key != "items"
        }

        created = []

        with transaction.atomic():
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValidationError(
                        {"items": {str(index): "Invalid item."}}
                    )

                payload = {**common, **item}
                serializer = self.get_serializer(data=payload)

                try:
                    serializer.is_valid(raise_exception=True)
                    created.append(
                        self.save_stock_aware_entry(serializer)
                    )
                except ValidationError as error:
                    raise ValidationError(
                        {"items": {str(index): error.detail}}
                    ) from error

        return Response(
            self.get_serializer(created, many=True).data,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        unlocked = self.get_object()

        with transaction.atomic():
            instance = (
                self.get_queryset()
                .select_for_update()
                .get(pk=unlocked.pk)
            )
            serializer = self.get_serializer(
                instance,
                data=request.data,
                partial=partial,
            )
            serializer.is_valid(raise_exception=True)

            if instance.stock_deducted:
                # Protect completed stock movement fields, but allow PATCH
                # requests that only update Event return/date information.
                # Earlier serializer logic populated item_type,
                # outward_type and quantity even when they were absent from
                # the PATCH body, causing valid Event returns to be rejected.
                protected_aliases = {
                    "component": {"component"},
                    "item_type": {"item_type", "itemType"},
                    "outward_type": {"outward_type", "typeOfOutward"},
                    "quantity": {
                        "quantity",
                        "no_of_components",
                        "noOfComponents",
                    },
                    "serial_numbers": {
                        "serial_numbers",
                        "serialNumbers",
                    },
                }

                attempted = set()

                for field_name, aliases in protected_aliases.items():
                    if not any(alias in request.data for alias in aliases):
                        continue

                    incoming = serializer.validated_data.get(field_name)

                    if field_name == "component":
                        incoming_value = getattr(incoming, "pk", incoming)
                        current_value = instance.component_id
                    elif field_name in {"item_type", "outward_type"}:
                        incoming_value = str(incoming or "").strip().upper()
                        current_value = str(
                            getattr(instance, field_name, "") or ""
                        ).strip().upper()
                    elif field_name == "quantity":
                        incoming_value = int(incoming or 0)
                        current_value = int(instance.quantity or 0)
                    else:
                        incoming_value = self.normalize_serials(incoming)
                        current_value = self.normalize_serials(
                            instance.serial_numbers
                        )

                    if incoming_value != current_value:
                        attempted.add(field_name)

                if attempted:
                    raise ValidationError(
                        {
                            "detail": (
                                "A completed stock movement cannot change: "
                                + ", ".join(sorted(attempted))
                            )
                        }
                    )

            save_values = {"approval_status": "NOT_REQUESTED"}

            is_event = str(instance.outward_type).upper() == "EVENT"
            is_component = str(instance.item_type).upper() == "COMPONENT"
            has_return_action = any(
                key in request.data
                for key in [
                    "returned_quantity",
                    "returnedQuantity",
                    "is_returned",
                    "isReturned",
                    "event_components",
                    "eventComponents",
                ]
            )

            if is_event and is_component and has_return_action:
                raw_target = request.data.get(
                    "returned_quantity",
                    request.data.get(
                        "returnedQuantity",
                        instance.returned_quantity,
                    ),
                )

                return_processed = bool(
                    request.data.get(
                        "is_returned",
                        request.data.get(
                            "isReturned",
                            instance.is_returned,
                        ),
                    )
                )

                # Compatibility with an old full-return PATCH.
                if (
                    "returned_quantity" not in request.data
                    and "returnedQuantity" not in request.data
                    and return_processed
                ):
                    raw_target = instance.quantity

                try:
                    target_returned = int(raw_target or 0)
                except (TypeError, ValueError) as error:
                    raise ValidationError(
                        {
                            "returned_quantity": (
                                "Returned quantity must be a whole number."
                            )
                        }
                    ) from error

                returned_serials = self.restore_event_component_stock(
                    instance,
                    target_returned,
                )

                if target_returned >= int(instance.quantity or 0):
                    movement_status = "RETURNED"
                    return_processed = True
                elif return_processed and target_returned > 0:
                    movement_status = "PARTIALLY_RETURNED"
                elif return_processed:
                    movement_status = "CLOSED_NOT_RETURNED"
                elif target_returned > 0:
                    movement_status = "PARTIALLY_RETURNED"
                else:
                    movement_status = "EVENT_OUT"

                save_values.update(
                    {
                        "returned_quantity": target_returned,
                        "returned_serial_numbers": returned_serials,
                        "stock_restored": (
                            target_returned
                            >= int(instance.quantity or 0)
                        ),
                        "is_returned": return_processed,
                        "status": movement_status,
                    }
                )

            elif is_event and has_return_action:
                return_processed = bool(
                    request.data.get(
                        "is_returned",
                        request.data.get(
                            "isReturned",
                            instance.is_returned,
                        ),
                    )
                )
                save_values.update(
                    {
                        "is_returned": return_processed,
                        "status": (
                            "RETURNED"
                            if return_processed
                            else "EVENT_OUT"
                        ),
                    }
                )

            instance = serializer.save(**save_values)

        return Response(self.get_serializer(instance).data)

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        if instance.stock_deducted:
            return Response(
                {
                    "detail": (
                        "This record changed In-Store stock and cannot be "
                        "deleted. Keep it as an audit record."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return super().destroy(request, *args, **kwargs)