from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import transaction
from inventory.cost_serializers import CostDetailsSerializerMixin
from rest_framework import serializers

from .models import (
    Inventory,
    InventoryReservation,
    ProjectInventory,
    SerialCostAllocation,
    SerialPurchaseCost,
    DroneInstance,
    DroneComponentAllocation,
)


class InventorySerializer(CostDetailsSerializerMixin, serializers.ModelSerializer):
    # Hidden only when ?summary=1 is used.
    # Normal/detail responses stay exactly as before.
    summary_exclude_fields = (
        "serial_numbers",
        "issued_serial_numbers",
    )
    basic_amount = serializers.SerializerMethodField()
    taxable_amount = serializers.SerializerMethodField()

    component_name = serializers.CharField(
        source="component.name",
        read_only=True,
        default="",
    )

    component_code = serializers.CharField(
        source="component.component_id",
        read_only=True,
        default="",
    )

    category_display = serializers.SerializerMethodField()

    class Meta:
        model = Inventory
        fields = [
            "id",
            "inventory_code",
            "component",
            "component_code",
            "component_name",
            "category",
            "category_display",
            "specifications",
            "component_type",
            "uom",
            "vendor",
            "purchase_order",
            "quantity",
            "unit_price",
            "basic_amount",
            "discount",
            "taxable_amount",
            "gst_percentage",
            "gst_amount",
            "freight_cost",
            "freight_gst_percentage",
            "freight_gst_amount",
            "round_off",
            "received_date",
            "total_price",
            "issued",
            "serial_numbers",
            "issued_serial_numbers",
            "created_at",
        ]

        extra_kwargs = {
            # Only Component is mandatory for manual Add Stock.
            "inventory_code": {
                "required": False,
                "allow_blank": True,
            },
            "category": {
                "required": False,
                "allow_blank": True,
                "allow_null": True,
            },
            "specifications": {
                "required": False,
                "allow_blank": True,
                "allow_null": True,
            },
            "component_type": {
                "required": False,
                "allow_blank": True,
                "allow_null": True,
            },
            "uom": {
                "required": False,
                "allow_blank": True,
                "allow_null": True,
            },
            "vendor": {
                "required": False,
                "allow_blank": True,
                "allow_null": True,
            },
            "purchase_order": {
                "required": False,
                "allow_blank": True,
                "allow_null": True,
            },
            "quantity": {
                "required": False,
            },
            "received_date": {
                "required": False,
                "allow_null": True,
            },
            "unit_price": {
                "required": False,
            },
            "discount": {
                "required": False,
            },
            "gst_percentage": {
                "required": False,
            },
            "gst_amount": {
                "required": False,
            },
            "freight_cost": {
                "required": False,
            },
            "freight_gst_percentage": {
                "required": False,
            },
            "freight_gst_amount": {
                "required": False,
            },
            "round_off": {
                "required": False,
            },
            "total_price": {
                "required": False,
            },
        }

        read_only_fields = [
            "component_code",
            "component_name",
            "category_display",
            "issued_serial_numbers",
            "created_at",
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

    @staticmethod
    def generate_serials(
        inventory_code,
        quantity,
        existing=None,
    ):
        result = list(existing or [])
        seen = set(result)

        raw_prefix = "".join(
            character
            for character in str(
                inventory_code or "INV"
            )
            if character.isalnum()
        ).upper() or "INV"

        index = 1

        while len(result) < max(
            int(quantity or 0),
            0,
        ):
            serial = (
                f"CINV_{raw_prefix}_"
                f"S{index:05d}"
            )
            index += 1

            if serial in seen:
                continue

            seen.add(serial)
            result.append(serial)

        return result

    @staticmethod
    def _money(value):
        try:
            return Decimal(
                str(value if value not in (None, "") else "0")
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        except (
            InvalidOperation,
            TypeError,
            ValueError,
        ):
            return Decimal("0.00")

    def get_basic_amount(self, obj):
        return f"{self._money(obj.basic_amount):.2f}"

    def get_taxable_amount(self, obj):
        return f"{self._money(obj.taxable_amount):.2f}"

    @staticmethod
    def _percent(value):
        try:
            parsed = Decimal(
                str(value if value not in (None, "") else "0")
            )
        except (
            InvalidOperation,
            TypeError,
            ValueError,
        ):
            parsed = Decimal("0")

        return min(
            Decimal("100"),
            max(
                Decimal("0"),
                parsed,
            ),
        )

    def get_category_display(self, obj):
        return (
            obj.category
            or getattr(
                obj.component,
                "category",
                "",
            )
            or ""
        )

    def validate_serial_numbers(self, value):
        return self.normalize_serials(value)

    def _fill_component_snapshot(self, validated_data):
        component = validated_data.get("component")

        if not component:
            return validated_data

        if not str(
            validated_data.get("category") or ""
        ).strip():
            validated_data["category"] = (
                getattr(component, "category", "")
                or ""
            )

        if not str(
            validated_data.get("specifications") or ""
        ).strip():
            validated_data["specifications"] = (
                getattr(
                    component,
                    "specifications",
                    "",
                )
                or ""
            )

        if not str(
            validated_data.get("component_type") or ""
        ).strip():
            validated_data["component_type"] = (
                getattr(
                    component,
                    "component_type",
                    "",
                )
                or ""
            )

        # UOM is intentionally manual for Add Stock.
        # Do not copy Component.unit_of_measurements automatically.

        return validated_data

    def _normalize_price_fields(self, validated_data):
        quantity = max(
            int(validated_data.get("quantity") or 1),
            1,
        )

        unit_price = self._money(
            validated_data.get("unit_price")
        )

        incoming_total = self._money(
            validated_data.get("total_price")
        )

        financial_keys = {
            "discount",
            "gst_percentage",
            "freight_cost",
            "freight_gst_percentage",
            "round_off",
        }

        has_financial_breakdown = any(
            key in validated_data
            for key in financial_keys
        )

        # Compatibility: older clients may send only Total Price.
        if (
            unit_price <= 0
            and incoming_total > 0
            and quantity > 0
            and not has_financial_breakdown
        ):
            unit_price = (
                incoming_total
                / Decimal(quantity)
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

        basic_amount = (
            unit_price
            * Decimal(quantity)
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        discount = min(
            basic_amount,
            max(
                Decimal("0.00"),
                self._money(
                    validated_data.get(
                        "discount"
                    )
                ),
            ),
        )

        taxable_amount = max(
            basic_amount - discount,
            Decimal("0.00"),
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        gst_percentage = self._percent(
            validated_data.get(
                "gst_percentage"
            )
        )

        gst_amount = (
            taxable_amount
            * gst_percentage
            / Decimal("100")
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        freight_cost = max(
            Decimal("0.00"),
            self._money(
                validated_data.get(
                    "freight_cost"
                )
            ),
        )

        freight_gst_percentage = self._percent(
            validated_data.get(
                "freight_gst_percentage"
            )
        )

        freight_gst_amount = (
            freight_cost
            * freight_gst_percentage
            / Decimal("100")
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        round_off = self._money(
            validated_data.get(
                "round_off"
            )
        )

        total_price = max(
            taxable_amount
            + gst_amount
            + freight_cost
            + freight_gst_amount
            + round_off,
            Decimal("0.00"),
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        validated_data.update(
            {
                "unit_price": unit_price,
                "discount": discount,
                "gst_percentage":
                    gst_percentage,
                "gst_amount": gst_amount,
                "freight_cost":
                    freight_cost,
                "freight_gst_percentage":
                    freight_gst_percentage,
                "freight_gst_amount":
                    freight_gst_amount,
                "round_off": round_off,
                "total_price":
                    total_price,
            }
        )

        return validated_data

    def _create_manual_cost_snapshot(self, stock):
        """
        Persist the complete Add Stock financial breakdown per serial.

        Every serial keeps its allocated Basic / Discount / GST /
        Freight / Freight GST / Round-Off / Grand Total values.
        """
        serials = self.normalize_serials(
            stock.serial_numbers
        )

        quantity = max(
            int(stock.quantity or 0),
            0,
        )

        if (
            quantity <= 0
            or len(serials) != quantity
        ):
            return None

        existing = (
            SerialCostAllocation.objects
            .filter(
                source_key=(
                    f"inventory:{stock.pk}"
                )
            )
            .first()
        )

        if existing is not None:
            return existing

        def split_money(total, count):
            total = self._money(total)

            if count <= 0:
                return []

            base = (
                total
                / Decimal(count)
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

            values = [
                base
                for _ in range(count)
            ]

            difference = (
                total
                - sum(
                    values,
                    Decimal("0.00"),
                )
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

            values[-1] = (
                values[-1]
                + difference
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

            return values

        basic_amount = self._money(
            stock.basic_amount
        )
        discount = self._money(
            stock.discount
        )
        gst_amount = self._money(
            stock.gst_amount
        )
        freight_cost = self._money(
            stock.freight_cost
        )
        freight_gst_amount = self._money(
            stock.freight_gst_amount
        )
        round_off = self._money(
            stock.round_off
        )
        grand_total = self._money(
            stock.total_price
        )

        basic_parts = split_money(
            basic_amount,
            quantity,
        )
        discount_parts = split_money(
            discount,
            quantity,
        )
        gst_parts = split_money(
            gst_amount,
            quantity,
        )
        freight_parts = split_money(
            freight_cost,
            quantity,
        )
        freight_gst_parts = split_money(
            freight_gst_amount,
            quantity,
        )
        round_off_parts = split_money(
            round_off,
            quantity,
        )
        grand_total_parts = split_money(
            grand_total,
            quantity,
        )

        units = []

        for index in range(quantity):
            units.append(
                {
                    "basic_amount":
                        f"{basic_parts[index]:.2f}",
                    "discount":
                        f"{discount_parts[index]:.2f}",
                    "gst_amount":
                        f"{gst_parts[index]:.2f}",
                    "freight_cost":
                        f"{freight_parts[index]:.2f}",
                    "freight_gst_amount":
                        f"{freight_gst_parts[index]:.2f}",
                    "round_off":
                        f"{round_off_parts[index]:.2f}",
                    "other_charges":
                        "0.00",
                    "grand_total":
                        f"{grand_total_parts[index]:.2f}",
                    "rounding_adjustment":
                        f"{round_off_parts[index]:.2f}",
                    "allocated_cost":
                        f"{grand_total_parts[index]:.2f}",
                    "unit_price":
                        f"{self._money(stock.unit_price):.2f}",
                    "gst_percentage":
                        str(stock.gst_percentage),
                    "freight_gst_percentage":
                        str(
                            stock.freight_gst_percentage
                        ),
                    "uom":
                        stock.uom or "",
                }
            )

        allocation = (
            SerialCostAllocation.objects.create(
                source_key=(
                    f"inventory:{stock.pk}"
                ),
                component=stock.component,
                source_inventory=stock,
                quantity=quantity,
                source_details={
                    "component_id":
                        stock.component_id,
                    "component_name":
                        stock.component.name,
                    "component_code":
                        stock.component.component_id,
                    "inventory_code":
                        stock.inventory_code,
                    "vendor_name":
                        stock.vendor or "",
                    "po_number":
                        stock.purchase_order or "",
                    "mr_number": "",
                    "received_date":
                        str(stock.received_date or ""),
                    "basis":
                        "Manual Add Stocks",
                    "category":
                        stock.category or "",
                    "specifications":
                        stock.specifications or "",
                    "component_type":
                        stock.component_type or "",
                    "uom":
                        stock.uom or "",
                    "quantity":
                        quantity,
                    "unit_price":
                        f"{self._money(stock.unit_price):.2f}",
                    "basic_amount":
                        f"{basic_amount:.2f}",
                    "discount":
                        f"{discount:.2f}",
                    "taxable_amount":
                        f"{self._money(stock.taxable_amount):.2f}",
                    "gst_percentage":
                        str(stock.gst_percentage),
                    "gst_amount":
                        f"{gst_amount:.2f}",
                    "freight_cost":
                        f"{freight_cost:.2f}",
                    "freight_gst_percentage":
                        str(
                            stock.freight_gst_percentage
                        ),
                    "freight_gst_amount":
                        f"{freight_gst_amount:.2f}",
                    "round_off":
                        f"{round_off:.2f}",
                    "grand_total":
                        f"{grand_total:.2f}",
                },
                units=units,
            )
        )

        SerialPurchaseCost.objects.bulk_create(
            [
                SerialPurchaseCost(
                    allocation=allocation,
                    component=stock.component,
                    serial_number=serial,
                    unit_index=index,
                    allocated_cost=(
                        grand_total_parts[
                            index
                        ]
                    ),
                )
                for index, serial
                in enumerate(serials)
            ]
        )

        return allocation

    @transaction.atomic
    def create(self, validated_data):
        validated_data = self._fill_component_snapshot(
            validated_data
        )
        validated_data = self._normalize_price_fields(
            validated_data
        )

        if not validated_data.get("inventory_code"):
            validated_data["inventory_code"] = (
                self._generate_next_inventory_code()
            )

        quantity = max(
            int(validated_data.get("quantity") or 1),
            1,
        )

        # Quantity is optional for manual Add Stock.
        # Blank/zero input is normalized to one stock unit.
        validated_data["quantity"] = quantity

        serials = self.normalize_serials(
            validated_data.get("serial_numbers")
        )

        if len(serials) < quantity:
            serials = self.generate_serials(
                validated_data["inventory_code"],
                quantity,
                existing=serials,
            )

        validated_data["serial_numbers"] = (
            serials[:quantity]
        )

        stock = super().create(
            validated_data
        )

        self._create_manual_cost_snapshot(
            stock
        )

        return stock

    @transaction.atomic
    def update(self, instance, validated_data):
        validated_data = self._fill_component_snapshot(
            validated_data
        )

        quantity = max(
            int(
                validated_data.get(
                    "quantity",
                    instance.quantity,
                )
                or 0
            ),
            0,
        )

        serials = self.normalize_serials(
            validated_data.get(
                "serial_numbers",
                instance.serial_numbers,
            )
        )

        if len(serials) < quantity:
            serials = self.generate_serials(
                instance.inventory_code,
                quantity,
                existing=serials,
            )

        validated_data["serial_numbers"] = (
            serials[:quantity]
        )

        # Recalculate total only when the client explicitly changes the
        # unit price. Existing serial purchase-cost snapshots remain immutable.
        if "unit_price" in validated_data:
            validated_data = self._normalize_price_fields(
                {
                    **validated_data,
                    "quantity": quantity,
                }
            )

        return super().update(
            instance,
            validated_data,
        )

    @staticmethod
    def _generate_next_inventory_code():
        last = Inventory.objects.order_by(
            "-id"
        ).first()

        last_no = 0

        if last and last.inventory_code:
            raw_code = str(
                last.inventory_code
            ).strip()

            for prefix in (
                "INV-",
                "INV",
            ):
                raw_code = raw_code.replace(
                    prefix,
                    "",
                )

            try:
                last_no = int(raw_code)
            except (
                ValueError,
                TypeError,
            ):
                last_no = 0

        return f"INV{last_no + 1:05d}"


class InventoryReservationSerializer(
    serializers.ModelSerializer
):
    material_request_number = (
        serializers.CharField(
            source=(
                "material_request."
                "material_request_id"
            ),
            read_only=True,
        )
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

    remaining_reserved_quantity = (
        serializers.IntegerField(
            read_only=True,
        )
    )

    active_reserved_quantity = (
        serializers.IntegerField(
            read_only=True,
        )
    )

    is_fully_issued = serializers.BooleanField(
        read_only=True,
    )

    class Meta:
        model = InventoryReservation

        fields = [
            "id",
            "material_request",
            "material_request_number",
            "component",
            "component_code",
            "component_name",
            "requested_quantity",
            "reserved_store_quantity",
            "procurement_shortage_quantity",
            "issued_store_quantity",
            "remaining_reserved_quantity",
            "active_reserved_quantity",
            "is_fully_issued",
            "status",
            "created_at",
            "updated_at",
        ]

        read_only_fields = fields


class ProjectInventorySerializer(CostDetailsSerializerMixin, serializers.ModelSerializer):
    # Large serial arrays are not needed by normal table rows.
    # Use /serial-options/ or the full detail endpoint when the user clicks.
    summary_exclude_fields = (
        "po_numbers",
        "inward_codes",
        "purchased_serial_numbers",
        "available_store_serials",
        "available_purchased_serials",
        "issued_store_serials",
        "issued_purchased_serials",
        "issued_serials",
    )
    material_request_number = serializers.CharField(
        source="material_request.material_request_id",
        read_only=True,
    )
    source_mr_number = serializers.CharField(
        source="material_request.material_request_id",
        read_only=True,
    )
    material_request_status = serializers.CharField(
        source="material_request.status",
        read_only=True,
    )
    requester_name = serializers.CharField(
        source="material_request.requester_name",
        read_only=True,
    )
    request_type = serializers.CharField(
        source="material_request.request_type",
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
    category = serializers.SerializerMethodField()
    specifications = serializers.SerializerMethodField()
    reserved_store_quantity = serializers.SerializerMethodField()
    procurement_shortage_quantity = serializers.SerializerMethodField()
    reservation_status = serializers.SerializerMethodField()
    available_store_serials = serializers.SerializerMethodField()
    available_purchased_serials = serializers.SerializerMethodField()
    issued_serials = serializers.SerializerMethodField()
    total_ready_quantity = serializers.IntegerField(read_only=True)
    calculated_issued_quantity = serializers.IntegerField(read_only=True)
    remaining_store_quantity = serializers.IntegerField(read_only=True)
    remaining_purchased_quantity = serializers.IntegerField(read_only=True)
    remaining_quantity = serializers.IntegerField(read_only=True)
    is_fulfilled = serializers.BooleanField(read_only=True)

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

    @staticmethod
    def generated_inventory_serials(stock_row):
        existing = ProjectInventorySerializer.normalize_serials(
            stock_row.serial_numbers
        )
        quantity = max(int(stock_row.quantity or 0), 0)
        prefix = "".join(
            character
            for character in str(stock_row.inventory_code or f"INV{stock_row.pk}")
            if character.isalnum()
        ).upper() or f"INV{stock_row.pk}"
        seen = set(existing) | set(
            ProjectInventorySerializer.normalize_serials(
                stock_row.issued_serial_numbers
            )
        )
        index = 1
        while len(existing) < quantity:
            serial = f"CINV_{prefix}_S{index:05d}"
            index += 1
            if serial in seen:
                continue
            seen.add(serial)
            existing.append(serial)
        return existing[:quantity]

    def _get_reservation(self, obj):
        cache = self.context.setdefault(
            "_project_reservation_cache",
            {},
        )

        cache_key = (
            int(obj.material_request_id),
            int(obj.component_id),
        )

        if cache_key in cache:
            return cache[cache_key]

        material_request = getattr(
            obj,
            "material_request",
            None,
        )

        if material_request is not None:
            prefetched_cache = getattr(
                material_request,
                "_prefetched_objects_cache",
                {},
            )

            # When the ViewSet prefetched inventory_reservations, the cached
            # relation is authoritative even when it is empty. Do not run a
            # fallback query for every ProjectInventory row with no reservation.
            if "inventory_reservations" in prefetched_cache:
                reservation = next(
                    (
                        row
                        for row in prefetched_cache.get(
                            "inventory_reservations",
                            [],
                        )
                        if row.component_id
                        == obj.component_id
                    ),
                    None,
                )

                cache[cache_key] = reservation
                return reservation

        reservation = (
            InventoryReservation.objects
            .filter(
                material_request_id=
                    obj.material_request_id,
                component_id=obj.component_id,
            )
            .first()
        )

        cache[cache_key] = reservation
        return reservation

    def get_category(self, obj):
        return getattr(obj.component, "category", "") or ""

    def get_specifications(self, obj):
        return (
            getattr(obj.component, "specifications", "")
            or getattr(obj.component, "specification", "")
            or ""
        )

    def get_reserved_store_quantity(self, obj):
        reservation = self._get_reservation(obj)
        if reservation is None:
            return int(obj.store_quantity or 0)
        return int(reservation.reserved_store_quantity or 0)

    def get_procurement_shortage_quantity(self, obj):
        reservation = self._get_reservation(obj)
        if reservation is None:
            return max(
                int(obj.requested_quantity or 0)
                - int(obj.store_quantity or 0),
                0,
            )
        return int(reservation.procurement_shortage_quantity or 0)

    def get_reservation_status(self, obj):
        reservation = self._get_reservation(obj)
        return str(reservation.status) if reservation is not None else ""

    def get_available_store_serials(self, obj):
        """
        Keep the normal Project Inventory list response lightweight.

        A component can have thousands of free In-Store serial numbers.
        Returning that same serial array on every ProjectInventory row makes
        the list response extremely large. Normal list requests therefore
        return an empty array unless the caller explicitly asks for store
        serials. The dedicated serial-options endpoint enables the flag only
        for the one ProjectInventory row the user is working with.
        """
        if not self.context.get("include_store_serials", False):
            return []

        cache = self.context.setdefault(
            "_available_store_serial_cache",
            {},
        )
        component_id = int(obj.component_id)

        if component_id not in cache:
            serials = []
            seen = set()

            stock_rows = (
                Inventory.objects
                .filter(
                    component_id=component_id,
                    issued=False,
                    quantity__gt=0,
                )
                .only(
                    "id",
                    "inventory_code",
                    "quantity",
                    "serial_numbers",
                    "issued_serial_numbers",
                    "received_date",
                )
                .order_by(
                    "received_date",
                    "id",
                )
            )

            for stock_row in stock_rows:
                for serial in self.generated_inventory_serials(
                    stock_row
                ):
                    if serial not in seen:
                        seen.add(serial)
                        serials.append(serial)

            cache[component_id] = serials

        return cache[component_id]

    def get_available_purchased_serials(self, obj):
        issued = set(self.normalize_serials(obj.issued_purchased_serials))
        return [
            serial
            for serial in self.normalize_serials(obj.purchased_serial_numbers)
            if serial not in issued
        ]

    def get_issued_serials(self, obj):
        return self.normalize_serials(
            self.normalize_serials(obj.issued_store_serials)
            + self.normalize_serials(obj.issued_purchased_serials)
        )

    class Meta:
        model = ProjectInventory
        fields = [
            "id",
            "material_request",
            "material_request_number",
            "source_mr_number",
            "material_request_status",
            "requester_name",
            "request_type",
            "project",
            "component",
            "component_code",
            "component_name",
            "category",
            "specifications",
            "requested_quantity",
            "reserved_store_quantity",
            "procurement_shortage_quantity",
            "store_quantity",
            "purchased_quantity",
            "qc_passed_quantity",
            "qc_failed_quantity",
            "quantity",
            "total_ready_quantity",
            "issued_store_quantity",
            "issued_purchased_quantity",
            "issued_quantity",
            "calculated_issued_quantity",
            "remaining_store_quantity",
            "remaining_purchased_quantity",
            "remaining_quantity",
            "is_fulfilled",
            "reservation_status",
            "po_numbers",
            "inward_codes",
            "purchased_serial_numbers",
            "available_store_serials",
            "available_purchased_serials",
            "issued_store_serials",
            "issued_purchased_serials",
            "issued_serials",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields



class DroneComponentAllocationSerializer(serializers.ModelSerializer):
    component_code = serializers.CharField(
        source="component.component_id", read_only=True, default=""
    )
    component_name = serializers.CharField(
        source="component.name", read_only=True, default=""
    )
    category = serializers.CharField(
        source="component.category", read_only=True, default=""
    )
    component_type = serializers.CharField(
        source="component.component_type", read_only=True, default=""
    )
    specification = serializers.SerializerMethodField()
    hsn_no = serializers.SerializerMethodField()
    uom = serializers.SerializerMethodField()

    class Meta:
        model = DroneComponentAllocation
        fields = [
            "id",
            "component",
            "component_code",
            "component_name",
            "category",
            "component_type",
            "specification",
            "hsn_no",
            "uom",
            "quantity",
            "serial_numbers",
            "source_details",
        ]
        read_only_fields = fields

    def get_specification(self, obj):
        component = obj.component
        return str(
            getattr(component, "specifications", "")
            or getattr(component, "specification", "")
            or ""
        )

    def get_hsn_no(self, obj):
        component = obj.component
        return str(
            getattr(component, "hsn_no", "")
            or getattr(component, "hsn_number", "")
            or getattr(component, "hsn_numbers", "")
            or ""
        )

    def get_uom(self, obj):
        details = obj.source_details if isinstance(obj.source_details, dict) else {}
        return str(
            details.get("uom")
            or getattr(obj.component, "unit", "")
            or getattr(obj.component, "uom", "")
            or getattr(obj.component, "unit_of_measurements", "")
            or ""
        )


class DroneInstanceSerializer(serializers.ModelSerializer):
    material_request_number = serializers.CharField(
        source="material_request.material_request_id", read_only=True
    )
    project = serializers.CharField(source="material_request.project", read_only=True)
    request_type = serializers.CharField(source="material_request.request_type", read_only=True)
    suffix = serializers.CharField(read_only=True)
    status_label = serializers.SerializerMethodField()
    replacement_mr_number = serializers.SerializerMethodField()
    component_allocations = DroneComponentAllocationSerializer(many=True, read_only=True)

    class Meta:
        model = DroneInstance
        fields = [
            "id",
            "material_request",
            "material_request_number",
            "project",
            "request_type",
            "sequence",
            "suffix",
            "instance_code",
            "status",
            "status_label",
            "replacement_material_request",
            "replacement_mr_number",
            "workflow_metadata",
            "component_allocations",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_status_label(self, obj):
        from .drone_instances import instance_status_label
        return instance_status_label(obj.status)

    def get_replacement_mr_number(self, obj):
        replacement = obj.replacement_material_request
        return str(getattr(replacement, "material_request_id", "") or "")
