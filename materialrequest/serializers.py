from django.db.models import Sum

from rest_framework import serializers

from inventory.models import (
    Inventory,
    InventoryReservation,
)

from .models import BOMItem, MaterialRequest, RDItem


class ReservationFieldsMixin:
    """
    Adds reservation quantities to BOM and R&D component rows.

    The values come from InventoryReservation, which is the source of
    truth for stock reserved for one Material Request + Component.
    """

    def _get_reservation(self, obj):
        material_request_id = getattr(
            obj,
            "material_request_id",
            None,
        )
        component_id = getattr(
            obj,
            "component_id",
            None,
        )

        if not material_request_id or not component_id:
            return None

        cache = self.context.setdefault(
            "_inventory_reservation_cache",
            {},
        )
        cache_key = (
            int(material_request_id),
            int(component_id),
        )

        if cache_key in cache:
            return cache[cache_key]

        material_request = getattr(
            obj,
            "material_request",
            None,
        )

        prefetched = []

        if material_request is not None:
            prefetched = list(
                getattr(
                    material_request,
                    "_prefetched_objects_cache",
                    {},
                ).get(
                    "inventory_reservations",
                    [],
                )
            )

        reservation = next(
            (
                row
                for row in prefetched
                if row.component_id == component_id
            ),
            None,
        )

        if reservation is None:
            reservation = (
                InventoryReservation.objects
                .filter(
                    material_request_id=(
                        material_request_id
                    ),
                    component_id=component_id,
                )
                .first()
            )

        cache[cache_key] = reservation
        return reservation

    def _get_live_inventory_availability(
        self,
        obj,
    ):
        """
        Calculate the quantity available to this Material Request using
        the same rule as Manager approval:

            physical unissued Inventory
            - active remaining reservations of other MRs
            = available quantity for this MR
        """
        material_request_id = getattr(
            obj,
            "material_request_id",
            None,
        )

        component_id = getattr(
            obj,
            "component_id",
            None,
        )

        if not component_id:
            return {
                "physical_quantity": 0,
                "reserved_by_other_mrs": 0,
                "available_quantity": 0,
            }

        cache = self.context.setdefault(
            "_live_inventory_availability_cache",
            {},
        )

        cache_key = (
            int(material_request_id or 0),
            int(component_id),
        )

        if cache_key in cache:
            return cache[cache_key]

        physical_quantity = (
            Inventory.objects
            .filter(
                component_id=component_id,
                issued=False,
                quantity__gt=0,
            )
            .aggregate(total=Sum("quantity"))
            .get("total")
            or 0
        )

        reservation_rows = (
            InventoryReservation.objects
            .filter(
                component_id=component_id,
                status__in=[
                    "ACTIVE",
                    "PARTIAL",
                ],
            )
            .exclude(
                material_request_id=(
                    material_request_id
                )
            )
            .values(
                "reserved_store_quantity",
                "issued_store_quantity",
            )
        )

        reserved_by_other_mrs = sum(
            max(
                int(
                    row[
                        "reserved_store_quantity"
                    ]
                    or 0
                )
                - int(
                    row[
                        "issued_store_quantity"
                    ]
                    or 0
                ),
                0,
            )
            for row in reservation_rows
        )

        availability = {
            "physical_quantity": max(
                int(physical_quantity or 0),
                0,
            ),
            "reserved_by_other_mrs": max(
                int(reserved_by_other_mrs or 0),
                0,
            ),
            "available_quantity": max(
                int(physical_quantity or 0)
                - int(
                    reserved_by_other_mrs or 0
                ),
                0,
            ),
        }

        cache[cache_key] = availability
        return availability

    def get_available_inventory_quantity(
        self,
        obj,
    ):
        return self._get_live_inventory_availability(
            obj
        )["available_quantity"]

    def get_physical_inventory_quantity(
        self,
        obj,
    ):
        return self._get_live_inventory_availability(
            obj
        )["physical_quantity"]

    def get_reserved_by_other_mrs(
        self,
        obj,
    ):
        return self._get_live_inventory_availability(
            obj
        )["reserved_by_other_mrs"]

    def get_reserved_by_other_mr_details(
        self,
        obj,
    ):
        """
        Return the active stock reservations that reduce availability for
        this MR component.

        This makes the API explain *why* available stock is lower than the
        physical In-Store quantity, e.g.:

            MR-260807-00001 -> 7 reserved
        """
        material_request_id = getattr(
            obj,
            "material_request_id",
            None,
        )
        component_id = getattr(
            obj,
            "component_id",
            None,
        )

        if not component_id:
            return []

        rows = (
            InventoryReservation.objects
            .select_related("material_request")
            .filter(
                component_id=component_id,
                status__in=[
                    "ACTIVE",
                    "PARTIAL",
                ],
            )
            .exclude(
                material_request_id=material_request_id
            )
            .order_by("created_at", "id")
        )

        details = []

        for reservation in rows:
            remaining = max(
                int(
                    reservation.reserved_store_quantity
                    or 0
                )
                - int(
                    reservation.issued_store_quantity
                    or 0
                ),
                0,
            )

            if remaining <= 0:
                continue

            details.append(
                {
                    "material_request_id": (
                        reservation
                        .material_request
                        .material_request_id
                    ),
                    "reserved_quantity": remaining,
                    "status": reservation.status,
                }
            )

        return details

    def get_reserved_store_quantity(self, obj):
        reservation = self._get_reservation(obj)

        return int(
            getattr(
                reservation,
                "reserved_store_quantity",
                0,
            )
            or 0
        )

    def get_procurement_shortage_quantity(self, obj):
        reservation = self._get_reservation(obj)

        return int(
            getattr(
                reservation,
                "procurement_shortage_quantity",
                0,
            )
            or 0
        )

    def get_issued_store_quantity(self, obj):
        reservation = self._get_reservation(obj)

        return int(
            getattr(
                reservation,
                "issued_store_quantity",
                0,
            )
            or 0
        )

    def get_remaining_reserved_quantity(self, obj):
        reservation = self._get_reservation(obj)

        if reservation is None:
            return 0

        return int(
            reservation.remaining_reserved_quantity
            or 0
        )

    def get_reservation_status(self, obj):
        reservation = self._get_reservation(obj)

        return (
            str(reservation.status)
            if reservation is not None
            else ""
        )


class BOMItemSerializer(
    ReservationFieldsMixin,
    serializers.ModelSerializer,
):
    material_request = serializers.PrimaryKeyRelatedField(
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

    available_inventory_quantity = (
        serializers.SerializerMethodField()
    )

    physical_inventory_quantity = (
        serializers.SerializerMethodField()
    )

    reserved_by_other_mrs = (
        serializers.SerializerMethodField()
    )

    reserved_by_other_mr_details = (
        serializers.SerializerMethodField()
    )

    reserved_store_quantity = (
        serializers.SerializerMethodField()
    )
    procurement_shortage_quantity = (
        serializers.SerializerMethodField()
    )
    issued_store_quantity = (
        serializers.SerializerMethodField()
    )
    remaining_reserved_quantity = (
        serializers.SerializerMethodField()
    )
    reservation_status = (
        serializers.SerializerMethodField()
    )

    class Meta:
        model = BOMItem

        exclude = (
            "unit_price",
            "price",
            "tax",
        )

        read_only_fields = (
            "material_request",
            "inventory_quantity",
            "available_inventory_quantity",
            "physical_inventory_quantity",
            "reserved_by_other_mrs",
            "reserved_by_other_mr_details",
            "po_raised_quantity",
            "delivered_quantity",
            "qc_passed_quantity",
            "qc_failed_quantity",
            "project_inventory_quantity",
            "reserved_store_quantity",
            "procurement_shortage_quantity",
            "issued_store_quantity",
            "remaining_reserved_quantity",
            "reservation_status",
        )


class RDItemSerializer(
    ReservationFieldsMixin,
    serializers.ModelSerializer,
):
    material_request = serializers.PrimaryKeyRelatedField(
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

    available_inventory_quantity = (
        serializers.SerializerMethodField()
    )

    physical_inventory_quantity = (
        serializers.SerializerMethodField()
    )

    reserved_by_other_mrs = (
        serializers.SerializerMethodField()
    )

    reserved_by_other_mr_details = (
        serializers.SerializerMethodField()
    )

    reserved_store_quantity = (
        serializers.SerializerMethodField()
    )
    procurement_shortage_quantity = (
        serializers.SerializerMethodField()
    )
    issued_store_quantity = (
        serializers.SerializerMethodField()
    )
    remaining_reserved_quantity = (
        serializers.SerializerMethodField()
    )
    reservation_status = (
        serializers.SerializerMethodField()
    )

    class Meta:
        model = RDItem

        exclude = (
            "unit_price",
            "price",
            "tax",
        )

        read_only_fields = (
            "material_request",
            "inventory_quantity",
            "available_inventory_quantity",
            "physical_inventory_quantity",
            "reserved_by_other_mrs",
            "reserved_by_other_mr_details",
            "po_raised_quantity",
            "delivered_quantity",
            "qc_passed_quantity",
            "qc_failed_quantity",
            "project_inventory_quantity",
            "reserved_store_quantity",
            "procurement_shortage_quantity",
            "issued_store_quantity",
            "remaining_reserved_quantity",
            "reservation_status",
        )


class MaterialRequestSerializer(
    serializers.ModelSerializer
):
    # The New Material Request page generates the business MR ID.
    # Keep that exact value writable on CREATE so it is stored in DB.
    material_request_id = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=True,
    )

    bom = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    bom_items = BOMItemSerializer(
        many=True,
        required=False,
    )

    rd_items = RDItemSerializer(
        many=True,
        required=False,
    )

    class Meta:
        model = MaterialRequest
        fields = "__all__"

    def validate_material_request_id(self, value):
        value = str(value or "").strip()

        if not value:
            raise serializers.ValidationError(
                "Material Request ID is required."
            )

        # Existing records keep their original business MR ID.
        if self.instance is not None:
            current_value = str(
                self.instance.material_request_id or ""
            ).strip()

            if value != current_value:
                raise serializers.ValidationError(
                    "Material Request ID cannot be changed after creation."
                )

            return current_value

        # Protect against duplicate IDs when two create pages are open.
        if MaterialRequest.objects.filter(
            material_request_id=value
        ).exists():
            raise serializers.ValidationError(
                "This Material Request ID already exists. Refresh the New Material Request page and submit again."
            )

        return value

    def validate(self, attrs):
        request_type = str(
            attrs.get(
                "request_type",
                getattr(
                    self.instance,
                    "request_type",
                    "BOM",
                ),
            )
            or ""
        ).strip().upper()

        bom = attrs.get(
            "bom",
            getattr(
                self.instance,
                "bom",
                None,
            ),
        )

        if request_type == "BOM" and not bom:
            raise serializers.ValidationError(
                {
                    "bom": [
                        "Please select a BOM."
                    ]
                }
            )

        return attrs

    def validate_status(self, value):
        allowed = {
            choice[0]
            for choice in (
                MaterialRequest
                ._meta
                .get_field("status")
                .choices
            )
        }

        if value not in allowed:
            raise serializers.ValidationError(
                "Invalid status."
            )

        return value

    def validate_approval_status(self, value):
        allowed = {
            choice[0]
            for choice in (
                MaterialRequest
                ._meta
                .get_field("approval_status")
                .choices
            )
        }

        if value not in allowed:
            raise serializers.ValidationError(
                "Invalid approval_status."
            )

        return value

    @staticmethod
    def get_creation_inventory_quantity(component):
        """
        Snapshot the physical central In-Store quantity at the exact time
        this MR item is created.

        InventoryReservation is intentionally NOT subtracted here:
        inventory_quantity means physical stock seen at MR creation.
        Reserved stock is reported separately by
        reserved_by_other_mrs / reserved_by_other_mr_details.
        """
        if component is None:
            return 0

        component_id = getattr(
            component,
            "pk",
            component,
        )

        return int(
            (
                Inventory.objects
                .filter(
                    component_id=component_id,
                    issued=False,
                    quantity__gt=0,
                )
                .aggregate(total=Sum("quantity"))
                .get("total")
                or 0
            )
        )

    def create(self, validated_data):
        bom_items = validated_data.pop(
            "bom_items",
            [],
        )
        rd_items = validated_data.pop(
            "rd_items",
            [],
        )

        if (
            str(
                validated_data.get(
                    "request_type",
                    "",
                )
            )
            .strip()
            .upper()
            != "BOM"
        ):
            validated_data["bom"] = None

        material_request = (
            MaterialRequest.objects.create(
                **validated_data
            )
        )

        for item in bom_items:
            BOMItem.objects.create(
                material_request=material_request,
                component=item.get("component"),
                category=item.get(
                    "category",
                    "",
                ),
                specification=item.get(
                    "specification",
                    "",
                ),
                quantity=item.get("quantity", 1),
                inventory_quantity=(
                    self.get_creation_inventory_quantity(
                        item.get("component")
                    )
                ),
                unit=item.get("unit", "pc"),
                unit_price=item.get(
                    "unit_price",
                    0,
                ),
                price=item.get("price", 0),
                tax=item.get("tax", 0),
                vendor=item.get(
                    "vendor",
                    "N/A",
                ),
                remarks=item.get(
                    "remarks",
                    "",
                ),
            )

        for item in rd_items:
            RDItem.objects.create(
                material_request=material_request,
                component=item.get("component"),
                category=item.get(
                    "category",
                    "",
                ),
                specifications=item.get(
                    "specifications",
                    "",
                ),
                quantity=item.get("quantity", 1),
                inventory_quantity=(
                    self.get_creation_inventory_quantity(
                        item.get("component")
                    )
                ),
                unit=item.get("unit", "pc"),
                unit_price=item.get(
                    "unit_price",
                    0,
                ),
                price=item.get("price", 0),
                tax=item.get("tax", 0),
                total_price=item.get(
                    "total_price",
                    0,
                ),
                vendor=item.get(
                    "vendor",
                    "N/A",
                ),
                remarks=item.get(
                    "remarks",
                    "",
                ),
            )

        return material_request

    def update(self, instance, validated_data):
        """
        Component rows are not modified by workflow PATCH calls.

        Manager-approved routing is deliberately handled in
        materialrequest/views.py after this serializer saves the new
        approval_status.
        """
        validated_data.pop("bom_items", None)
        validated_data.pop("rd_items", None)

        approval_status = validated_data.get(
            "approval_status"
        )
        explicit_status = validated_data.get(
            "status"
        )
        po_raised = validated_data.get(
            "po_raised"
        )

        for attribute, value in (
            validated_data.items()
        ):
            setattr(instance, attribute, value)

        if (
            po_raised
            and explicit_status is None
            and instance.status not in {
                "PO_DELIVERED",
                "QC_CHECKED",
                "PROJECT_INVENTORY_READY",
                "INVENTORY_ISSUED",
                "MR_COMPLETED",
            }
        ):
            instance.status = "PO_RAISED"

        if approval_status == "REQUESTED":
            instance.status = "REQUESTED"

        elif approval_status == "PENDING_MANAGER":
            instance.status = "PENDING_MANAGER"

        elif approval_status == "MANAGER_APPROVED":
            # Do not set the MR route here. The view reserves stock and
            # chooses INVENTORY_PENDING or PROCUREMENT_PENDING.
            pass

        elif approval_status == "MANAGER_REJECTED":
            instance.status = "MANAGER_REJECTED"

        elif approval_status == "PO_RAISED":
            instance.status = "PO_RAISED"

        if explicit_status:
            instance.status = explicit_status

        instance.save()
        return instance