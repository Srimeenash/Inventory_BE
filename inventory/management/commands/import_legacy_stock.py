"""Import current In-Store stock from Stock Verification (never its old copy).

Dry-run is the default. An --apply imports all valid rows atomically, and
refuses to write if a row needs review unless --skip-blocked was requested.
"""

import csv
from collections import Counter
from decimal import Decimal
from pathlib import Path
import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from components.models import Component
from components.serializers import ComponentSerializer
from inventory.legacy_stock_import import (
    load_stock_rows, normalized_description, text,
)
from inventory.models import Inventory
from inventory.serializers import InventorySerializer


def safe_cell(value):
    # The report is opened in Excel; treat all text as data, not formulas.
    value = text(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def component_data(row):
    raw_hsn = text(row.raw.get("HSN Code"))
    return {
        "category": row.category,
        "component_type": row.component_type[:150],
        "specifications": row.specifications,
        "unit_of_measurements": row.uom[:100],
        "hsn_numbers": raw_hsn if re.fullmatch(r"[0-9]{4,8}", raw_hsn) else "",
        "sku_numbers": text(row.raw.get("SKU Number"))[:100],
        "tally_reference": text(row.raw.get("Tally Ref"))[:100],
        "unit_price": row.unit_price,
    }


def stock_data(row, component, code):
    return {
        "inventory_code": code,
        "component": component.pk,
        "category": row.category,
        "component_type": row.component_type[:150],
        "specifications": row.specifications,
        "uom": row.uom[:100],
        "vendor": text(row.raw.get("Vendor Name"))[:255],
        "purchase_order": text(row.raw.get("Purchase Order"))[:255],
        "quantity": row.quantity,
        "unit_price": row.unit_price,
        "gst_percentage": row.gst_percent,
        "discount": Decimal("0"),
        "freight_cost": Decimal("0"),
        "freight_gst_percentage": Decimal("0"),
        "round_off": Decimal("0"),
        "received_date": row.received_date,
        "issued": False,
    }


def read_map(path):
    if not path:
        return {}
    mapping = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"old_id", "new_id"}.issubset(reader.fieldnames or []):
            raise CommandError("Component map needs old_id,new_id CSV headers.")
        for line, entry in enumerate(reader, start=2):
            old = text(entry.get("old_id")).upper()
            new = text(entry.get("new_id")).upper()
            if not old and not new:
                continue
            if not old or not new or old in mapping and mapping[old] != new:
                raise CommandError(f"Invalid/conflicting component mapping at CSV line {line}.")
            mapping[old] = new
    return mapping


def export_review(path, items):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "Excel row", "Old Comp ID", "IPMS Component ID", "Category",
        "Project label", "Verification", "In Inv (current stock)", "Unit Price", "GST percent", "Inward Date",
        "Result", "Issue", "Warning", "Description", "Rack", "Box",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row, result, issue, warnings, component_id in items:
            writer.writerow({
                "Excel row": row.excel_row,
                "Old Comp ID": safe_cell(row.legacy_id),
                "IPMS Component ID": safe_cell(component_id),
                "Category": safe_cell(row.category),
                "Project label": safe_cell(row.raw.get("Project")),
                "Verification": safe_cell(row.verification),
                "In Inv (current stock)": row.quantity if row.quantity is not None else safe_cell(row.raw.get("In Inv")),
                "Unit Price": row.unit_price if row.unit_price is not None else "",
                "GST percent": row.gst_percent if row.gst_percent is not None else "",
                "Inward Date": row.received_date.isoformat() if row.received_date else "",
                "Result": result,
                "Issue": safe_cell("; ".join(issue)),
                "Warning": safe_cell("; ".join(warnings)),
                "Description": safe_cell(row.description),
                "Rack": safe_cell(row.raw.get("Rack No")),
                "Box": safe_cell(row.raw.get("Box No")),
            })
    return path


class Command(BaseCommand):
    help = "Review or import current stock from the legacy Stock Verification sheet."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the supplied .xlsx workbook")
        parser.add_argument("--report", default="inventory_import_review.csv")
        parser.add_argument("--component-map", help="Optional CSV: old_id,new_id (existing IPMS component)")
        parser.add_argument("--create-components", action="store_true", help="Create new category-coded IPMS components")
        parser.add_argument("--scope", choices=("store-only", "all-positive"), default="store-only",
                            help="Default imports only Project=INVENTORY; all-positive explicitly includes project-labelled stock")
        parser.add_argument("--store-label", action="append", default=[],
                            help="Additional exact Project label to treat as shared store stock (repeat as needed)")
        parser.add_argument("--skip-blocked", action="store_true", help="Allow a partial import; blocked rows remain unimported")
        parser.add_argument("--apply", action="store_true", help="Write to the configured database after the dry run")

    def handle(self, *args, **options):
        file = Path(options["file"])
        if not file.is_file():
            raise CommandError(f"Workbook not found: {file}")
        mapping = read_map(options["component_map"])
        try:
            rows, ignored = load_stock_rows(file, today=timezone.localdate())
        except (ValueError, OSError, KeyError) as exc:
            raise CommandError(f"Cannot read stock workbook: {exc}") from exc

        components = list(Component.objects.all())
        by_legacy = {c.legacy_inventory_id.upper(): c for c in components if c.legacy_inventory_id}
        by_code = {c.component_id.upper(): c for c in components}
        # Exact specifications help prevent accidentally duplicating an
        # existing component under a different new category-based ID.
        by_spec = {}
        for component in components:
            key = (component.category, normalized_description(component.specifications))
            if key[1]:
                by_spec.setdefault(key, []).append(component)
        current_stock = {
            x.legacy_source_key: x
            for x in Inventory.objects.exclude(legacy_source_key__isnull=True)
            .exclude(legacy_source_key="").only("legacy_source_key", "legacy_source_data", "component_id")
        }
        review = []
        ready = []
        store_labels = {"INVENTORY", *(text(label).upper() for label in options["store_label"])}
        for row in rows:
            errors = list(row.errors)
            warnings = list(row.warnings)
            project_label = text(row.raw.get("Project")).upper()
            old = current_stock.get(row.source_key)
            if old:
                previous = (old.legacy_source_data or {}).get("source_fingerprint")
                if previous == row.fingerprint:
                    review.append((row, "ALREADY IMPORTED", [], warnings, old.component.component_id if old.component_id else ""))
                else:
                    review.append((row, "BLOCKED", ["Source row changed after earlier import; reconcile manually"], warnings, ""))
                continue
            if options["scope"] == "store-only" and project_label not in store_labels:
                warnings.append(f"Project label {project_label or '(blank)'} excluded from shared store scope")
                review.append((row, "EXCLUDED PROJECT LABEL", errors, warnings, ""))
                continue

            mapped_code = mapping.get(row.legacy_id)
            component = by_code.get(mapped_code) if mapped_code else by_legacy.get(row.legacy_id)
            if mapped_code and component is None:
                errors.append(f"Component map target {mapped_code} does not exist in IPMS")
            if component is None and row.legacy_id in by_code:
                component = by_code[row.legacy_id]
            if component is not None:
                if component.category != row.category:
                    errors.append("Existing IPMS component category differs from the spreadsheet")
                if component.legacy_inventory_id and component.legacy_inventory_id.upper() != row.legacy_id:
                    errors.append("Existing IPMS component is mapped to another old ID")
                if (
                    not mapped_code and component.legacy_inventory_id is None
                    and normalized_description(component.specifications)
                    and normalized_description(row.specifications)
                    and normalized_description(component.specifications) != normalized_description(row.specifications)
                ):
                    errors.append("Existing component with this ID has different specifications")
            elif row.legacy_id:
                possible = by_spec.get((row.category, normalized_description(row.specifications)), [])
                if possible:
                    errors.append("Possible existing component with these specifications; use --component-map")
                elif not options["create_components"]:
                    errors.append("Component not found; rerun with --create-components or map to an existing one")
                else:
                    serializer = ComponentSerializer(data=component_data(row))
                    if not serializer.is_valid():
                        errors.append(f"Component fields invalid: {serializer.errors}")

            if errors:
                review.append((row, "BLOCKED", errors, warnings, component.component_id if component else ""))
            else:
                review.append((row, "READY", [], warnings, component.component_id if component else "(create new)"))
                ready.append((row, component))

        report = export_review(options["report"], review)
        count = Counter(item[1] for item in review)
        self.stdout.write(f"Sheet: Stock Verification | positive stock rows: {len(rows)} | zero/blank stock rows skipped: {ignored}")
        self.stdout.write(f"Scope: {options['scope']} | Ready: {count['READY']} | Blocked: {count['BLOCKED']} | Excluded by project label: {count['EXCLUDED PROJECT LABEL']} | Already imported: {count['ALREADY IMPORTED']}")
        self.stdout.write(f"Review report: {report}")
        if not options["apply"]:
            self.stdout.write("Dry run only. Database unchanged.")
            return
        if count["BLOCKED"] and not options["skip_blocked"]:
            raise CommandError("No data imported: fix blocked rows, or explicitly use --skip-blocked for a partial import.")
        if not ready:
            self.stdout.write("No new rows to import.")
            return

        # A database error rolls back every newly created component, stock row
        # and serial cost entry from this run.
        try:
            with transaction.atomic():
                used_codes = set(Inventory.objects.values_list("inventory_code", flat=True))
                sequence = max(
                    (int(match.group(1)) for code in used_codes
                     if (match := re.fullmatch(r"INV-?(\d+)", str(code or ""), flags=re.I))),
                    default=0,
                )
                created_components = {}
                affected = set()
                imported = 0
                for row, component in ready:
                    if component is None:
                        component = created_components.get(row.legacy_id)
                    if component is None:
                        serializer = ComponentSerializer(data=component_data(row))
                        serializer.is_valid(raise_exception=True)
                        component = serializer.save(
                            name=row.description[:255],
                            legacy_inventory_id=row.legacy_id,
                        )
                        by_code[component.component_id.upper()] = component
                        created_components[row.legacy_id] = component
                    elif component.legacy_inventory_id is None:
                        component.legacy_inventory_id = row.legacy_id
                        component.save(update_fields=["legacy_inventory_id"])

                    while True:
                        sequence += 1
                        code = f"INV{sequence:05d}"
                        if code not in used_codes:
                            used_codes.add(code)
                            break
                    stock = InventorySerializer(data=stock_data(row, component, code))
                    stock.is_valid(raise_exception=True)
                    stock.save(legacy_source_key=row.source_key, legacy_source_data=row.metadata)
                    affected.add(component.pk)
                    imported += 1

                # The component list and MR stock check must agree on physical
                # inventory after the import.
                for component in Component.objects.filter(pk__in=affected):
                    component.stock_quantity = (
                        Inventory.objects.filter(component=component, issued=False)
                        .aggregate(total=Sum("quantity"))["total"] or 0
                    )
                    component.save(update_fields=["stock_quantity"])
        except Exception as exc:
            raise CommandError(f"Import rolled back: {exc}") from exc

        imported_codes = {
            item.legacy_source_key: item.component.component_id
            for item in Inventory.objects.filter(
                legacy_source_key__in=[row.source_key for row, _ in ready]
            ).select_related("component")
        }
        review = [
            (row, "IMPORTED", issues, warnings, imported_codes[row.source_key])
            if result == "READY" and row.source_key in imported_codes
            else (row, result, issues, warnings, component_id)
            for row, result, issues, warnings, component_id in review
        ]
        export_review(report, review)
        self.stdout.write(self.style.SUCCESS(f"Imported {imported} stock rows. Review any BLOCKED rows in {report}."))
