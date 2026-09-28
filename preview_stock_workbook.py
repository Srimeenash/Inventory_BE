"""Standalone source-only review; does not connect to or write a database."""

import argparse
import csv
from datetime import date
from pathlib import Path

from inventory.legacy_stock_import import load_stock_rows, text


def excel_text(value):
    value = text(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--out", type=Path, default=Path("inventory_source_review.csv"))
    args = parser.parse_args()
    rows, skipped = load_stock_rows(args.workbook, today=date.today())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "Excel row", "Old Comp ID", "Category", "In Inv", "Unit Price",
            "Inward Date", "Project label", "Verification", "Source result", "Issues to fix", "Warnings",
            "Description", "Rack", "Box",
        ])
        for row in rows:
            writer.writerow([
                row.excel_row, excel_text(row.legacy_id), excel_text(row.category),
                row.quantity if row.quantity is not None else excel_text(row.raw.get("In Inv")),
                row.unit_price if row.unit_price is not None else "",
                row.received_date or "", excel_text(row.raw.get("Project")), excel_text(row.verification),
                "BLOCKED" if row.errors else "SOURCE READY (scope undecided)",
                excel_text("; ".join(row.errors)), excel_text("; ".join(row.warnings)),
                excel_text(row.description), excel_text(row.raw.get("Rack No")),
                excel_text(row.raw.get("Box No")),
            ])
    print(f"Positive stock rows: {len(rows)}; blocked: {sum(bool(row.errors) for row in rows)}; zero/blank stock rows skipped: {skipped}")
    print(f"Source review: {args.out}")


if __name__ == "__main__":
    main()
