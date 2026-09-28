"""Attach workbook purchase totals for display without changing available stock.

Dry run by default. Reads the original Stock Verification workbook, including
old batches whose In Inv is zero. No Inventory quantity or cost is modified.
"""

import csv
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.historical_stock_quantities import source_quantities, stock_identity
from inventory.legacy_stock_import import decimal, normalized_description, text
from inventory.models import Inventory
from inventory.views import invalidate_inventory_cache


def safe_cell(value):
    value = text(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


class Command(BaseCommand):
    help = "Display original Excel Qty (including issued batches) without increasing stock available for issue."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Original Stock Verification .xlsx workbook")
        parser.add_argument("--report", default="inventory_quantity_display_review.csv")
        parser.add_argument("--apply", action="store_true", help="Save display metadata after reviewing the dry run")

    def handle(self, *args, **options):
        workbook = Path(options["file"])
        if not workbook.is_file():
            raise CommandError(f"Workbook not found: {workbook}")
        try:
            totals, by_row, invalid = source_quantities(workbook)
        except (ValueError, OSError, TypeError) as exc:
            raise CommandError(f"Cannot read original workbook: {exc}") from exc

        groups = defaultdict(list)
        for inventory in Inventory.objects.filter(
            legacy_source_key__startswith="stock-verification:"
        ).only("id", "legacy_source_key", "legacy_source_data"):
            metadata = inventory.legacy_source_data or {}
            raw = metadata.get("original_values") or {}
            identity = stock_identity(raw) if isinstance(raw, dict) else None
            if identity:
                groups[identity].append(inventory)

        updates = []
        report_rows = []
        for identity, stocks in sorted(groups.items()):
            stocks.sort(key=lambda item: item.pk)
            total = totals.get(identity)
            problems = []
            if identity in invalid:
                problems.append("At least one workbook Qty is missing, negative, or fractional")
            if total is None or total <= 0:
                problems.append("No positive original Qty for this component")

            imported_specs = {
                normalized_description(
                    (stock.legacy_source_data or {}).get("original_values", {})
                    .get("Component Specification") or
                    (stock.legacy_source_data or {}).get("original_values", {})
                    .get("Description")
                )
                for stock in stocks
            }
            if len(imported_specs) > 1:
                problems.append("Imported batches have different specifications; review this component")

            imported_start = Decimal("0")
            for stock in stocks:
                metadata = stock.legacy_source_data or {}
                original = metadata.get("original_values") or {}
                try:
                    excel_row = int(metadata.get("excel_row") or
                                    stock.legacy_source_key.rsplit(":", 1)[-1])
                except (ValueError, TypeError):
                    excel_row = -1
                if by_row.get(excel_row) != identity:
                    problems.append(f"Imported Excel row {excel_row} does not match this workbook")
                in_inv = decimal(original.get("In Inv"))
                if in_inv is None:
                    problems.append(f"Imported Excel row {excel_row} has no saved In Inv")
                else:
                    imported_start += in_inv
            if total is not None and total < imported_start:
                problems.append("Original Qty is below the quantity imported from this component")

            result = "BLOCKED" if problems else "READY"
            report_rows.append({
                "Component ID": safe_cell(identity[0]),
                "Description": safe_cell((stocks[0].legacy_source_data or {})
                                         .get("original_values", {}).get("Description")),
                "Workbook Qty": int(total) if total is not None else "",
                "In Inv at import": str(imported_start),
                "Imported batches": len(stocks),
                "Result": result,
                "Issue": safe_cell("; ".join(problems)),
            })
            if problems:
                # A later corrected workbook can invalidate an earlier
                # display sync. Fall back to each row's own saved Excel Qty.
                for stock in stocks:
                    metadata = stock.legacy_source_data or {}
                    if "historical_display_quantity" in metadata:
                        cleaned = dict(metadata)
                        cleaned.pop("historical_display_quantity")
                        updates.append((stock.pk, cleaned))
                continue

            # Place the entire group total on only one imported batch. The
            # frontend sums batches, so the total must not be repeated.
            for index, stock in enumerate(stocks):
                metadata = stock.legacy_source_data or {}
                quantity = int(total) if index == 0 else 0
                if metadata.get("historical_display_quantity") != quantity:
                    updates.append((stock.pk, {**metadata, "historical_display_quantity": quantity}))

        report = Path(options["report"])
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=(
                "Component ID", "Description", "Workbook Qty", "In Inv at import",
                "Imported batches", "Result", "Issue",
            ))
            writer.writeheader()
            writer.writerows(report_rows)

        blocked = sum(item["Result"] == "BLOCKED" for item in report_rows)
        self.stdout.write(
            f"Read-only Qty review: {len(report_rows) - blocked} ready groups, "
            f"{blocked} blocked groups; {len(updates)} stock rows to update."
        )
        self.stdout.write(f"Review report: {report}")
        if not options["apply"]:
            self.stdout.write("Dry run only. Database unchanged.")
            return

        with transaction.atomic():
            for stock_id, metadata in updates:
                Inventory.objects.filter(pk=stock_id).update(legacy_source_data=metadata)
            if updates:
                transaction.on_commit(invalidate_inventory_cache)
        self.stdout.write(
            f"Updated display metadata for {len(updates)} stock rows. "
            "Available quantity, serials and cost were unchanged."
        )
