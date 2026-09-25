from inventory.cost_serializers import CostDetailsSerializerMixin
from django.db.models import Sum

from rest_framework import serializers
from bom.models import BOM as ProductBOM, ProjectBOM

from inventory.models import (
    Inventory,
    InventoryReservation,
)

from .models import BOMItem, MaterialRequest, RDItem, RequestItem


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

        if material_request is not None:
            prefetched_cache = getattr(
                material_request,
                "_prefetched_objects_cache",
                {},
            )

            # The MaterialRequest ViewSet prefetches inventory_reservations
            # for normal/detail responses. An empty prefetched relation means
            # there is no matching reservation, so do not issue a fallback query.
            if "inventory_reservations" in prefetched_cache:
                reservation = next(
                    (
                        row
                        for row in prefetched_cache.get(
                            "inventory_reservations",
                            [],
                        )
                        if row.component_id
                        == component_id
                    ),
                    None,
                )

                cache[cache_key] = reservation
                return reservation

        reservation = (
            InventoryReservation.objects
            .filter(
                material_request_id=
                    material_request_id,
                component_id=component_id,
            )
            .first()
        )

        cache[cache_key] = reservation
        return reservation

    def _get_component_inventory_snapshot(
        self,
        component_id,
    ):
        """
        Load physical stock plus active reservations once per component
        for the whole serializer response.

        Older code executed an Inventory aggregate and an
        InventoryReservation query for every MR + Component pair.
        """
        component_id = int(component_id)

        cache = self.context.setdefault(
            "_component_inventory_snapshot_cache",
            {},
        )

        if component_id in cache:
            return cache[component_id]

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

        reservation_rows = list(
            InventoryReservation.objects
            .select_related("material_request")
            .filter(
                component_id=component_id,
                status__in=[
                    "ACTIVE",
                    "PARTIAL",
                ],
            )
            .order_by(
                "created_at",
                "id",
            )
        )

        snapshot = {
            "physical_quantity": max(
                int(physical_quantity or 0),
                0,
            ),
            "reservations": reservation_rows,
        }

        cache[component_id] = snapshot
        return snapshot

    def _get_live_inventory_availability(
        self,
        obj,
    ):
        """
        Calculate:
            physical unissued Inventory
            - active remaining reservations of other MRs
            = available quantity for this MR

        Stock/reservation rows are reused from the per-component cache.
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

        snapshot = self._get_component_inventory_snapshot(
            component_id
        )

        reserved_by_other_mrs = 0

        for reservation in snapshot["reservations"]:
            if (
                reservation.material_request_id
                == material_request_id
            ):
                continue

            reserved_by_other_mrs += max(
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

        physical_quantity = snapshot[
            "physical_quantity"
        ]

        availability = {
            "physical_quantity":
                physical_quantity,
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
        Return active reservations that reduce availability for this
        MR component, reusing the same per-component snapshot used by
        the availability fields.
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

        details_cache = self.context.setdefault(
            "_reserved_by_other_mr_details_cache",
            {},
        )

        cache_key = (
            int(material_request_id or 0),
            int(component_id),
        )

        if cache_key in details_cache:
            return details_cache[cache_key]

        snapshot = self._get_component_inventory_snapshot(
            component_id
        )

        details = []

        for reservation in snapshot["reservations"]:
            if (
                reservation.material_request_id
                == material_request_id
            ):
                continue

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
                    "reserved_quantity":
                        remaining,
                    "status":
                        reservation.status,
                }
            )

        details_cache[cache_key] = details
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

    component_type = serializers.CharField(
        source="component.component_type",
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

    component_type = serializers.CharField(
        source="component.component_type",
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




class RequestItemSerializer(
    ReservationFieldsMixin,
    serializers.ModelSerializer,
):
    material_request = serializers.PrimaryKeyRelatedField(read_only=True)
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

    component_type = serializers.CharField(
        source="component.component_type",
        read_only=True,
        default="",
    )
    available_inventory_quantity = serializers.SerializerMethodField()
    physical_inventory_quantity = serializers.SerializerMethodField()
    reserved_by_other_mrs = serializers.SerializerMethodField()
    reserved_by_other_mr_details = serializers.SerializerMethodField()
    reserved_store_quantity = serializers.SerializerMethodField()
    procurement_shortage_quantity = serializers.SerializerMethodField()
    issued_store_quantity = serializers.SerializerMethodField()
    remaining_reserved_quantity = serializers.SerializerMethodField()
    reservation_status = serializers.SerializerMethodField()

    class Meta:
        model = RequestItem
        fields = "__all__"
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
    CostDetailsSerializerMixin, serializers.ModelSerializer
):
    # Fast table mode:
    # /material-requests/?summary=1
    #
    # Nested component rows are intentionally omitted from the list.
    # Fetch the normal detail endpoint when the user opens one MR.
    summary_exclude_fields = (
        "bom_items",
        "rd_items",
        "request_items",
    )
    # Actual logged-in user who created this MR.
    # Backend sets this from request.user.
    requester = serializers.PrimaryKeyRelatedField(
        read_only=True
    )

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

    request_items = RequestItemSerializer(
        many=True,
        required=False,
    )
    # Includes source row IDs and deletion reasons; deleted rows are not MR items.
    custom_bom_items = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True,
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
                getattr(self.instance, "request_type", "BOM"),
            )
            or ""
        ).strip().upper()

        if request_type not in {
            "BOM",
            "R&D",
            "RETURNABLE",
            "RETAIL_SALES",
        }:
            raise serializers.ValidationError(
                {"request_type": ["Invalid request type."]}
            )

        project = str(
            attrs.get(
                "project",
                getattr(self.instance, "project", ""),
            )
            or ""
        ).strip()

        bom = attrs.get(
            "bom",
            getattr(self.instance, "bom", None),
        )

        if request_type in {"BOM", "R&D"} and not project:
            raise serializers.ValidationError(
                {"project": ["Project is required for BOM and R&D requests."]}
            )

        if request_type == "BOM" and not bom:
            raise serializers.ValidationError(
                {"bom": ["Please select a BOM."]}
            )

        if request_type != "BOM":
            attrs["bom"] = None
            attrs["customized_bom"] = False

        if request_type in {"RETURNABLE", "RETAIL_SALES"}:
            attrs["project"] = ""

        if request_type == "RETURNABLE":
            purpose = str(
                attrs.get(
                    "returnable_purpose",
                    getattr(self.instance, "returnable_purpose", ""),
                )
                or ""
            ).strip().upper()

            # Every Returnable purpose may create a NEW MR when the user
            # chooses Components on the New Material Request page.
            #
            # Drone mode does not call this serializer at all; it reuses an
            # existing In-Drone MR through componentusage/move-from-in-drone.
            valid_purposes = {
                choice[0]
                for choice in MaterialRequest.RETURNABLE_PURPOSE_CHOICES
            }

            if purpose not in valid_purposes:
                raise serializers.ValidationError(
                    {
                        "returnable_purpose": [
                            "Please select a valid Returnable purpose."
                        ]
                    }
                )

            remarks = str(
                attrs.get(
                    "remarks",
                    getattr(self.instance, "remarks", ""),
                )
                or ""
            ).strip()

            if not remarks:
                raise serializers.ValidationError(
                    {"remarks": ["Remarks are mandatory for Returnable requests."]}
                )

            request_date = attrs.get(
                "date",
                getattr(self.instance, "date", None),
            )
            return_date = attrs.get(
                "required_date",
                getattr(self.instance, "required_date", None),
            )

            if request_date and return_date:
                if return_date < request_date:
                    raise serializers.ValidationError(
                        {"required_date": ["Returnable date cannot be before request date."]}
                    )

                if purpose in {
                    "FLIGHT_TEST",
                    "QC_CHECK",
                    "MISCELLANEOUS_USAGE",
                } and (return_date - request_date).days > 4:
                    raise serializers.ValidationError(
                        {"required_date": ["This Returnable purpose allows a maximum of 4 days."]}
                    )
        else:
            attrs["returnable_purpose"] = ""

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
        custom_bom_items = validated_data.pop("custom_bom_items", [])
        bom_items = validated_data.pop(
            "bom_items",
            [],
        )
        rd_items = validated_data.pop(
            "rd_items",
            [],
        )
        request_items = validated_data.pop(
            "request_items",
            [],
        )

        is_custom_bom = (
            validated_data.get("request_type") == "BOM"
            and validated_data.get("customized_bom") is True
        )
        source_bom = None
        original_by_id = {}
        if is_custom_bom:
            reference = str(validated_data.get("bom") or "").strip()
            source_bom = (
                ProductBOM.objects.filter(pk=int(reference)).first()
                if reference.isdigit()
                else ProductBOM.objects.filter(bom_number=reference).first()
            )
            if source_bom is None:
                raise serializers.ValidationError({"bom": "Source Product BOM was not found."})
            if not custom_bom_items:
                raise serializers.ValidationError({
                    "custom_bom_items": "Include the Custom BOM rows, including deleted rows."
                })
            original_by_id = {
                item.pk: item
                for item in source_bom.items.select_related("component").all()
            }
            referenced_ids = [
                row.get("source_bom_item_id")
                for row in custom_bom_items
                if row.get("source_bom_item_id") not in (None, "")
            ]
            try:
                referenced_ids = [int(value) for value in referenced_ids]
            except (TypeError, ValueError):
                raise serializers.ValidationError({
                    "custom_bom_items": "Invalid source BOM line ID."
                })
            if (len(set(referenced_ids)) != len(referenced_ids)
                    or set(referenced_ids) != set(original_by_id)):
                raise serializers.ValidationError({
                    "custom_bom_items": "Every original Product BOM line must appear exactly once."
                })
            if sum(row.get("change_type") != "DELETED" for row in custom_bom_items) != len(bom_items):
                raise serializers.ValidationError({
                    "custom_bom_items": "Custom BOM rows do not match the submitted MR components."
                })
            if not bom_items:
                raise serializers.ValidationError({
                    "bom_items": "A Custom BOM needs at least one active component."
                })

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
                unit=str(item.get("unit") or "").strip(),
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

        if is_custom_bom:
            snapshot = []
            live_items = iter(bom_items)
            multiplier = int(material_request.required_quantity or 1)
            for index, row in enumerate(custom_bom_items):
                original = original_by_id.get(int(row["source_bom_item_id"])) if row.get("source_bom_item_id") not in (None, "") else None
                change_type = str(row.get("change_type") or "").upper()
                if change_type not in {"NEW", "EDITED", "UNCHANGED", "DELETED"}:
                    raise serializers.ValidationError({"custom_bom_items": "Invalid change type."})
                if change_type == "DELETED":
                    if original is None:
                        raise serializers.ValidationError({"custom_bom_items": "Deleted lines must come from the source BOM."})
                    reason = str(row.get("remarks") or "").strip()
                    if not reason or reason.lower() in {"none", "null"}:
                        raise serializers.ValidationError({"custom_bom_items": "Deleted lines require a reason."})
                    component = original.component
                    snapshot.append({
                        "source_bom_item_id": original.pk,
                        "component_id": component.pk if component else None,
                        "component_code": (component.component_id if component else original.component_code) or "",
                        "component_name": str(getattr(component, "name", "") or ""),
                        "category": original.category or getattr(component, "category", "") or "",
                        "component_type": getattr(component, "component_type", "") or "",
                        "specifications": original.specifications or "",
                        "quantity": original.quantity * multiplier,
                        "unit": original.unit or "",
                        "vendor": original.vendor or "",
                        "remarks": reason,
                        "change_type": "DELETED",
                        "position": index,
                    })
                    continue

                item = next(live_items)
                component = item.get("component")
                try:
                    submitted_component = int(row.get("component"))
                except (TypeError, ValueError):
                    raise serializers.ValidationError({"custom_bom_items": "Invalid component in the snapshot."})
                if component is None or submitted_component != component.pk or int(row.get("quantity") or 0) != int(item.get("quantity") or 0):
                    raise serializers.ValidationError({
                        "custom_bom_items": "Snapshot component or quantity differs from the MR item."
                    })
                if original is None and change_type != "NEW":
                    raise serializers.ValidationError({"custom_bom_items": "New lines must be marked NEW."})
                if original is not None:
                    changed = (
                        original.component_id != component.pk
                        or original.quantity * multiplier != int(item.get("quantity") or 0)
                        or (original.unit or "").strip() != str(item.get("unit") or "").strip()
                        or (original.category or getattr(original.component, "category", "") or "").strip() != str(item.get("category") or "").strip()
                        or (original.specifications or getattr(original.component, "specifications", "") or "").strip() != str(item.get("specification") or "").strip()
                    )
                    change_type = "EDITED" if changed or change_type == "EDITED" else "UNCHANGED"
                reason = str(item.get("remarks") or "").strip()
                if change_type == "EDITED" and (not reason or reason.lower() in {"none", "null"}):
                    raise serializers.ValidationError({"custom_bom_items": "Edited lines require a reason."})
                snapshot.append({
                    "source_bom_item_id": original.pk if original else None,
                    "component_id": component.pk,
                    "component_code": component.component_id,
                    "component_name": component.name or "",
                    "category": item.get("category") or component.category or "",
                    "component_type": component.component_type or "",
                    "specifications": item.get("specification") or component.specifications or "",
                    "quantity": int(item.get("quantity") or 0),
                    "unit": str(item.get("unit") or "").strip(),
                    "vendor": str(item.get("vendor") or ""),
                    "remarks": reason,
                    "change_type": change_type,
                    "position": index,
                })

            ProjectBOM.objects.create(
                material_request=material_request,
                material_request_number=material_request.material_request_id,
                status_snapshot=material_request.status,
                approval_status_snapshot=material_request.approval_status,
                source_bom=source_bom,
                source_bom_number=source_bom.bom_number,
                bom_name=f"{source_bom.bom_name or source_bom.product_name} - Customized",
                product_name=source_bom.product_name,
                version=source_bom.version,
                project=material_request.project,
                created_by=material_request.requester_name,
                items_snapshot=snapshot,
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
                unit=str(item.get("unit") or "").strip(),
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

        for item in request_items:
            RequestItem.objects.create(
                material_request=material_request,
                component=item.get("component"),
                category=item.get("category", ""),
                specifications=item.get("specifications", ""),
                quantity=item.get("quantity", 1),
                inventory_quantity=(
                    self.get_creation_inventory_quantity(
                        item.get("component")
                    )
                ),
                unit=str(item.get("unit") or "").strip(),
                vendor=item.get("vendor", "N/A"),
                remarks=item.get("remarks", ""),
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
        validated_data.pop("custom_bom_items", None)
        validated_data.pop("rd_items", None)
        validated_data.pop("request_items", None)

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
