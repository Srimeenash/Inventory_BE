from rest_framework import serializers

from .models import (
    Inventory,
    InventoryReservation,
    ProjectInventory,
)


class InventorySerializer(serializers.ModelSerializer):
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
            "vendor",
            "purchase_order",
            "quantity",
            "received_date",
            "total_price",
            "issued",
            "serial_numbers",
            "issued_serial_numbers",
            "created_at",
        ]
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
    def generate_serials(inventory_code, quantity, existing=None):
        result = list(existing or [])
        seen = set(result)
        raw_prefix = "".join(
            character
            for character in str(inventory_code or "INV")
            if character.isalnum()
        ).upper() or "INV"
        index = 1
        while len(result) < max(int(quantity or 0), 0):
            serial = f"CINV_{raw_prefix}_S{index:05d}"
            index += 1
            if serial in seen:
                continue
            seen.add(serial)
            result.append(serial)
        return result

    def get_category_display(self, obj):
        return (
            obj.category
            or getattr(obj.component, "category", "")
            or ""
        )

    def validate_serial_numbers(self, value):
        return self.normalize_serials(value)

    def create(self, validated_data):
        if not validated_data.get("inventory_code"):
            validated_data["inventory_code"] = (
                self._generate_next_inventory_code()
            )

        quantity = max(int(validated_data.get("quantity") or 0), 0)
        serials = self.normalize_serials(
            validated_data.get("serial_numbers")
        )
        if len(serials) < quantity:
            serials = self.generate_serials(
                validated_data["inventory_code"],
                quantity,
                existing=serials,
            )
        validated_data["serial_numbers"] = serials[:quantity]
        return super().create(validated_data)

    def update(self, instance, validated_data):
        quantity = max(
            int(validated_data.get("quantity", instance.quantity) or 0),
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
        validated_data["serial_numbers"] = serials[:quantity]
        return super().update(instance, validated_data)

    @staticmethod
    def _generate_next_inventory_code():
        last = Inventory.objects.order_by("-id").first()
        last_no = 0
        if last and last.inventory_code:
            raw_code = str(last.inventory_code).strip()
            for prefix in ("INV-", "INV"):
                raw_code = raw_code.replace(prefix, "")
            try:
                last_no = int(raw_code)
            except (ValueError, TypeError):
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


class ProjectInventorySerializer(serializers.ModelSerializer):
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
        cache = self.context.setdefault("_project_reservation_cache", {})
        cache_key = (int(obj.material_request_id), int(obj.component_id))
        if cache_key in cache:
            return cache[cache_key]
        material_request = getattr(obj, "material_request", None)
        prefetched = []
        if material_request is not None:
            prefetched = list(
                getattr(material_request, "_prefetched_objects_cache", {}).get(
                    "inventory_reservations", []
                )
            )
        reservation = next(
            (row for row in prefetched if row.component_id == obj.component_id),
            None,
        )
        if reservation is None:
            reservation = InventoryReservation.objects.filter(
                material_request_id=obj.material_request_id,
                component_id=obj.component_id,
            ).first()
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
        cache = self.context.setdefault("_available_store_serial_cache", {})
        component_id = int(obj.component_id)
        if component_id not in cache:
            serials = []
            seen = set()
            stock_rows = Inventory.objects.filter(
                component_id=component_id,
                issued=False,
                quantity__gt=0,
            ).order_by("received_date", "id")
            for stock_row in stock_rows:
                for serial in self.generated_inventory_serials(stock_row):
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
