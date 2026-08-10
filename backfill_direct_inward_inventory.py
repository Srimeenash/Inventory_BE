from django.db import transaction
from django.db.models import Sum

from inward.models import InwardEntry
from inventory.models import Inventory


def row_quantity(row):
    if not isinstance(row, dict):
        return 0

    raw_value = (
        row.get("qty")
        if row.get("qty") is not None
        else row.get("quantity")
        if row.get("quantity") is not None
        else row.get("passed_quantity")
        if row.get("passed_quantity") is not None
        else 1
    )

    try:
        return max(int(raw_value), 0)
    except (TypeError, ValueError):
        return 0


def passed_quantity(inward):
    return sum(
        row_quantity(row)
        for row in (
            inward.qc_passed_rows
            or []
        )
    )


created_count = 0
updated_count = 0
skipped_mr_stock = 0


with transaction.atomic():
    inwards = (
        InwardEntry.objects
        .select_for_update()
        .select_related(
            "component",
            "vendor",
            "purchase_order",
        )
        .prefetch_related("line_items")
        .filter(
            qc_status__in=[
                "COMPLETED",
                "PASS",
            ]
        )
        .order_by("received_date", "id")
    )

    for inward in inwards:
        source_mr_number = ""

        if inward.purchase_order:
            source_mr_number = str(
                inward.purchase_order
                .source_mr_number
                or ""
            ).strip()

        if source_mr_number:
            skipped_mr_stock += 1
            continue

        passed = passed_quantity(inward)

        if passed <= 0:
            continue

        inventory_code = str(
            inward.code or ""
        ).strip()

        if not inventory_code:
            continue

        existing = (
            Inventory.objects
            .select_for_update()
            .filter(
                inventory_code=inventory_code
            )
            .first()
        )

        vendor_name = (
            getattr(
                inward.vendor,
                "name",
                "",
            )
            or getattr(
                inward.vendor,
                "vendor_name",
                "",
            )
            or str(inward.vendor or "")
        )

        po_number = (
            getattr(
                inward.purchase_order,
                "po_number",
                "",
            )
            if inward.purchase_order
            else ""
        )

        total_price = (
            inward.line_items.aggregate(
                total=Sum("grand_total")
            ).get("total")
            or 0
        )

        values = {
            "component":
                inward.component,
            "category": (
                getattr(
                    inward.component,
                    "category",
                    "",
                )
                or ""
            ),
            "vendor": vendor_name,
            "purchase_order": po_number,
            "received_date":
                inward.received_date,
            "total_price": total_price,
        }

        if existing is None:
            Inventory.objects.create(
                inventory_code=inventory_code,
                quantity=passed,
                issued=False,
                **values,
            )

            created_count += 1
            continue

        for field_name, value in values.items():
            setattr(
                existing,
                field_name,
                value,
            )

        # Preserve already-issued stock if this script is rerun.
        existing.quantity = min(
            max(
                int(existing.quantity or 0),
                0,
            ),
            passed,
        )
        existing.issued = (
            existing.quantity == 0
        )

        existing.save(
            update_fields=[
                "component",
                "category",
                "vendor",
                "purchase_order",
                "received_date",
                "total_price",
                "quantity",
                "issued",
            ]
        )

        updated_count += 1


print(
    {
        "created_inventory_rows":
            created_count,
        "updated_inventory_rows":
            updated_count,
        "skipped_mr_linked_inwards":
            skipped_mr_stock,
    }
)

print("\nPhysical In-Store totals:")

totals = (
    Inventory.objects
    .filter(
        issued=False,
        quantity__gt=0,
    )
    .values(
        "component__component_id",
        "component__name",
    )
    .annotate(total=Sum("quantity"))
    .order_by(
        "component__component_id"
    )
)

for row in totals:
    print(row)