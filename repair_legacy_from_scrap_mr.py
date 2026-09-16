"""
ONE-TIME REPAIR FOR AN OLD LEGACY FROM-SCRAP MR

Your screenshot shows:
    MR-20260825-142546
    Status = MR_COMPLETED

That is an old legacy row created before the corrected missing-quantity
routing. New code will not automatically rewrite historical database rows.

This helper reconstructs the FULL source BOM/R&D rows and reroutes only the
missing quantity.

SAFETY:
- It refuses to run if the target MR already has linked Purchase Orders.
- Take a database backup first.

RUN FROM DJANGO PROJECT ROOT:

    python manage.py shell < repair_legacy_from_scrap_mr.py
"""

import re

from django.db import transaction

from inventory.models import (
    InventoryReservation,
    ProjectInventory,
)
from materialrequest.models import (
    MaterialRequest,
    BOMItem,
    RDItem,
)
from materialrequest.views import (
    MaterialRequestViewSet,
)
from outward.models import OutwardEntry
from procurement.models import PurchaseOrder


TARGET_MR = "MR-20260825-142546"


def normalize_serials(values):
    result = []
    seen = set()

    for value in values or []:
        serial = str(
            value or ""
        ).strip()

        if (
            serial
            and serial not in seen
        ):
            seen.add(serial)
            result.append(serial)

    return result


with transaction.atomic():
    target = (
        MaterialRequest.objects
        .select_for_update()
        .filter(
            material_request_id=(
                TARGET_MR
            )
        )
        .first()
    )

    if target is None:
        raise RuntimeError(
            f"{TARGET_MR} was not found."
        )

    if PurchaseOrder.objects.filter(
        source_mr_number=(
            TARGET_MR
        )
    ).exists():
        raise RuntimeError(
            "Repair stopped: this MR already has linked Purchase Orders. "
            "Do not rewrite component rows after Procurement has started."
        )

    remarks = str(
        target.remarks or ""
    )

    original_match = re.search(
        r"From\s+Scrap\s+for\s+(MR-[A-Za-z0-9_-]+)",
        remarks,
        flags=re.IGNORECASE,
    )

    if not original_match:
        raise RuntimeError(
            "Original MR number was not found in the From-Scrap remarks."
        )

    original_mr_number = (
        original_match
        .group(1)
        .strip()
    )

    source_mr = (
        MaterialRequest.objects
        .select_for_update()
        .filter(
            material_request_id=(
                original_mr_number
            )
        )
        .first()
    )

    if source_mr is None:
        raise RuntimeError(
            f"Original MR {original_mr_number} was not found."
        )

    linked_scrap = None

    candidates = (
        OutwardEntry.objects
        .select_for_update()
        .filter(
            outward_type="SCRAP"
        )
        .order_by("-id")
    )

    for candidate in candidates:
        metadata = (
            candidate.inventory_allocations
            if isinstance(
                candidate.inventory_allocations,
                dict,
            )
            else {}
        )

        replacement_number = str(
            metadata.get(
                "replacement_mr_number",
                "",
            )
            or ""
        ).strip()

        replacement_id = str(
            metadata.get(
                "replacement_mr_id",
                "",
            )
            or ""
        ).strip()

        if (
            replacement_number
            == TARGET_MR
            or replacement_id
            == str(target.id)
        ):
            linked_scrap = candidate
            break

    if linked_scrap is None:
        raise RuntimeError(
            "Linked Scrap workflow metadata was not found for this MR."
        )

    metadata = (
        linked_scrap
        .inventory_allocations
        or {}
    )

    recovered_items = (
        metadata.get("good_items")
        or metadata.get(
            "reorder_items"
        )
        or metadata.get(
            "selected_items"
        )
        or []
    )

    recovered_by_component = {}

    for item in recovered_items:
        if not isinstance(
            item,
            dict,
        ):
            continue

        component_key = str(
            item.get(
                "component"
            )
            or ""
        ).strip()

        if not component_key:
            continue

        recovered_by_component[
            component_key
        ] = normalize_serials(
            recovered_by_component
            .get(
                component_key,
                [],
            )
            + normalize_serials(
                item.get(
                    "serial_numbers"
                )
                or []
            )
        )

    source_is_rd = (
        str(
            source_mr.request_type
            or ""
        )
        .strip()
        .upper()
        in {
            "R&D",
            "RD",
        }
    )

    source_manager = (
        source_mr.rd_items
        if source_is_rd
        else source_mr.bom_items
    )

    item_model = (
        RDItem
        if source_is_rd
        else BOMItem
    )

    source_items = list(
        source_manager
        .select_related(
            "component"
        )
        .all()
        .order_by("id")
    )

    if not source_items:
        raise RuntimeError(
            "Original MR has no source component rows."
        )

    # Safe because the script already stopped when linked POs exist.
    target.bom_items.all().delete()
    target.rd_items.all().delete()

    InventoryReservation.objects.filter(
        material_request=target
    ).delete()

    ProjectInventory.objects.filter(
        material_request=target
    ).delete()

    for source_item in source_items:
        recovered_serials = (
            recovered_by_component
            .get(
                str(
                    source_item
                    .component_id
                ),
                [],
            )
        )

        clone = item_model()

        for field in (
            source_item
            ._meta
            .concrete_fields
        ):
            if (
                field.primary_key
                or field.name
                == "material_request"
            ):
                continue

            compatible = any(
                candidate.name
                == field.name
                for candidate
                in clone
                ._meta
                .concrete_fields
            )

            if compatible:
                setattr(
                    clone,
                    field.attname,
                    getattr(
                        source_item,
                        field.attname,
                    ),
                )

        clone.material_request = (
            target
        )

        for field_name in (
            "po_raised_quantity",
            "delivered_quantity",
            "qc_passed_quantity",
            "qc_failed_quantity",
            "project_inventory_quantity",
        ):
            if hasattr(
                clone,
                field_name,
            ):
                setattr(
                    clone,
                    field_name,
                    0,
                )

        if hasattr(
            clone,
            "inventory_quantity",
        ):
            clone.inventory_quantity = 0

        marker = (
            "FROM_SCRAP_SERIALS:"
            + "|".join(
                recovered_serials
            )
            + f"\nSOURCE_SCRAP:{linked_scrap.code}"
            + f"\nSOURCE_MR:{source_mr.material_request_id}"
        )

        if hasattr(
            clone,
            "remarks",
        ):
            clone.remarks = marker

        clone.save()

    # Restore original structure/type.
    target.request_type = (
        source_mr.request_type
    )
    target.bom = source_mr.bom
    target.customized_bom = (
        source_mr.customized_bom
    )
    target.project = (
        source_mr.project
    )
    target.required_quantity = (
        source_mr.required_quantity
    )

    # Finance already approved the Scrap, so reroute directly.
    target.status = (
        "INVENTORY_PENDING"
    )
    target.approval_status = (
        "FINANCE_APPROVED"
    )
    target.po_raised = False

    target.save(
        update_fields=[
            "request_type",
            "bom",
            "customized_bom",
            "project",
            "required_quantity",
            "status",
            "approval_status",
            "po_raised",
        ]
    )

    MaterialRequestViewSet().route_after_manager_approval(
        target,
        approval_source="FINANCE",
    )

    target.refresh_from_db()

    print(
        "\nREPAIR COMPLETE"
    )
    print(
        "MR:",
        target.material_request_id,
    )
    print(
        "Original MR:",
        source_mr.material_request_id,
    )
    print(
        "New Status:",
        target.status,
    )

    print(
        "\nCOMPONENT ROUTING"
    )

    for reservation in (
        InventoryReservation.objects
        .filter(
            material_request=target
        )
        .select_related(
            "component"
        )
        .order_by("id")
    ):
        print(
            reservation
            .component
            .component_id,
            "| Missing after recovery:",
            reservation.requested_quantity,
            "| In Store reserved:",
            reservation.reserved_store_quantity,
            "| Procurement shortage:",
            reservation.procurement_shortage_quantity,
        )
