"""
Backfill serial numbers for existing Inventory and ProjectInventory rows.

Place this file in the Django backend root, beside manage.py.

Run after the inventory migration:

    python manage.py shell -c "exec(open('backfill_inventory_serial_numbers.py', encoding='utf-8').read())"

Existing issued MR serials cannot always be reconstructed exactly. This script
creates or restores serials only from available QC and inventory information.
"""

from django.db import transaction

from inventory.models import (
    Inventory,
    ProjectInventory,
)
from inward.models import InwardEntry


def normalize(values):
    result = []
    seen = set()

    for value in values if isinstance(values, list) else []:
        serial = str(value or "").strip()

        if serial and serial not in seen:
            seen.add(serial)
            result.append(serial)

    return result


def row_quantity(row):
    try:
        return max(
            int(
                row.get(
                    "qty",
                    row.get("quantity", 1),
                )
            ),
            0,
        )
    except (TypeError, ValueError):
        return 0


def inward_serials(inward):
    if inward is None:
        return []

    result = []
    seen = set()
    row_no = 0

    digits = "".join(
        ch
        for ch in str(
            inward.code or inward.id
        )
        if ch.isdigit()
    )[-5:].zfill(5)

    for row in inward.qc_passed_rows or []:
        if not isinstance(row, dict):
            continue

        qty = row_quantity(row)

        raw = str(
            row.get("serialNumber")
            or row.get("serial_number")
            or row.get("serial")
            or ""
        ).strip()

        for offset in range(qty):
            row_no += 1

            serial = (
                raw
                if qty == 1
                else (
                    f"{raw}-{offset + 1}"
                    if raw
                    else ""
                )
            )

            serial = (
                serial
                or f"C_{digits}S{row_no:05d}"
            )

            if serial not in seen:
                seen.add(serial)
                result.append(serial)

    return result


def generated(stock, quantity, existing):
    result = list(existing)

    seen = (
        set(result)
        | set(
            normalize(
                stock.issued_serial_numbers
            )
        )
    )

    prefix = "".join(
        ch
        for ch in str(
            stock.inventory_code
            or f"INV{stock.pk}"
        )
        if ch.isalnum()
    ).upper() or f"INV{stock.pk}"

    index = 1

    while len(result) < quantity:
        serial = (
            f"CINV_{prefix}_S{index:05d}"
        )
        index += 1

        if serial in seen:
            continue

        seen.add(serial)
        result.append(serial)

    return result[:quantity]


with transaction.atomic():
    print(
        "Backfilling central Inventory serials..."
    )

    updated = 0

    inventory_rows = (
        Inventory.objects
        .select_for_update()
        .all()
        .order_by("id")
    )

    for stock in inventory_rows:
        quantity = max(
            int(stock.quantity or 0),
            0,
        )

        current = normalize(
            stock.serial_numbers
        )

        if len(current) == quantity:
            continue

        inward = (
            InwardEntry.objects
            .filter(
                code=stock.inventory_code
            )
            .first()
        )

        candidates = (
            inward_serials(inward)
            if inward
            else []
        )

        issued = set(
            normalize(
                stock.issued_serial_numbers
            )
        )

        candidates = [
            serial
            for serial in candidates
            if serial not in issued
        ]

        serials = candidates[:quantity]

        if len(serials) < quantity:
            serials = generated(
                stock,
                quantity,
                serials,
            )

        stock.serial_numbers = serials

        stock.save(
            update_fields=[
                "serial_numbers",
            ]
        )

        updated += 1

        print(
            stock.id,
            stock.inventory_code,
            len(serials),
        )

    print(
        f"Inventory serial backfill complete. "
        f"Updated {updated} row(s)."
    )

    print(
        "Backfilling MR-linked Project Inventory QC serials..."
    )

    project_updated = 0

    project_rows = (
        ProjectInventory.objects
        .select_for_update()
        .select_related(
            "material_request",
            "component",
        )
        .all()
        .order_by("id")
    )

    for project_row in project_rows:
        mr_number = str(
            project_row
            .material_request
            .material_request_id
            or ""
        ).strip()

        related_inwards = (
            InwardEntry.objects
            .filter(
                purchase_order__source_mr_number=mr_number,
                component_id=(
                    project_row.component_id
                ),
                removed_from_inventory=False,
            )
            .order_by(
                "received_date",
                "id",
            )
        )

        purchased = []
        seen = set()

        for inward in related_inwards:
            for serial in inward_serials(
                inward
            ):
                if serial not in seen:
                    seen.add(serial)
                    purchased.append(serial)

        changed = []

        if purchased != normalize(
            project_row
            .purchased_serial_numbers
        ):
            project_row.purchased_serial_numbers = (
                purchased
            )

            changed.append(
                "purchased_serial_numbers"
            )

        issued_purchased_quantity = int(
            project_row
            .issued_purchased_quantity
            or 0
        )

        if (
            not normalize(
                project_row
                .issued_purchased_serials
            )
            and issued_purchased_quantity > 0
            and purchased
        ):
            project_row.issued_purchased_serials = (
                purchased[
                    :issued_purchased_quantity
                ]
            )

            changed.append(
                "issued_purchased_serials"
            )

        if changed:
            project_row.save(
                update_fields=changed
            )

            project_updated += 1

            print(
                project_row
                .material_request
                .material_request_id,
                project_row.component_id,
                len(
                    project_row
                    .purchased_serial_numbers
                ),
                len(
                    project_row
                    .issued_purchased_serials
                ),
            )

    print(
        f"Project serial backfill complete. "
        f"Updated {project_updated} row(s)."
    )

print(
    "Serial-number backfill completed successfully."
)