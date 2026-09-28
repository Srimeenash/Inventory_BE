"""Parse the old Stock Verification workbook without modifying the database."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
import re

from openpyxl import load_workbook
from openpyxl.utils.datetime import from_excel


SOURCE_SHEET = "Stock Verification"
CATEGORY_MAP = {
    "ACCESSORIES": "ACCESSORIES",
    "AIRFRAME": "AIRFRAMES",
    "AIRFRAMES": "AIRFRAMES",
    "COMMUNICATION": "COMMUNICATION",
    "ELECTRICALS": "ELECTRICALS",
    "ELECTRONICS": "ELECTRONICS",
    "PAYLOAD": "PAYLOAD",
    "TOOLS": "TOOLS",
}
MISSING_IDS = {"", "-", "NA", "N/A", "NONE", "NULL"}
EXPECTED = {
    "Category", "Comp Type", "Comp ID", "Description",
    "Component Specification", "In Inv", "Qty", "UOM",
    "Unit Price", "GST %", "Inward Date",
}
CENT = Decimal("0.01")


def text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def decimal(value):
    if value is None or text(value) == "":
        return None
    try:
        number = Decimal(text(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not Decimal(str(value)).is_finite():
        return text(value)
    return value


def normalized_description(value):
    return " ".join(re.findall(r"[a-z0-9]+", text(value).lower()))


@dataclass
class StockRow:
    excel_row: int
    source_key: str
    raw: dict
    fingerprint: str
    legacy_id: str
    category: str
    verification: str
    description: str
    specifications: str
    component_type: str
    uom: str
    quantity: int | None
    unit_price: Decimal | None
    gst_percent: Decimal | None
    received_date: date | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def metadata(self):
        return {
            "source_sheet": SOURCE_SHEET,
            "excel_row": self.excel_row,
            "source_fingerprint": self.fingerprint,
            "verification": self.verification,
            "original_values": self.raw,
            "cost_note": "Unit price and GST use current stock quantity. Original shipping, round-off and full-batch totals are retained in original_values only.",
            "serial_note": "Original workbook has no serial numbers; IPMS generates internal stock serials.",
        }


def parse_date(value, workbook_epoch):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        converted = from_excel(value, epoch=workbook_epoch)
        return converted.date() if isinstance(converted, datetime) else converted
    if text(value):
        try:
            return date.fromisoformat(text(value))
        except ValueError:
            try:
                return datetime.fromisoformat(text(value)).date()
            except ValueError:
                return None
    return None


def fill_key(cell):
    fill = cell.fill
    if fill.patternType != "solid":
        return None
    color = fill.fgColor
    if color.type == "rgb":
        return ("rgb", color.rgb)
    if color.type == "theme":
        return ("theme", color.theme, round(color.tint, 6))
    return None


def load_stock_rows(filename, today):
    workbook = load_workbook(filename, read_only=True, data_only=True)
    try:
        if SOURCE_SHEET not in workbook.sheetnames:
            raise ValueError(f"Sheet '{SOURCE_SHEET}' is missing.")
        sheet = workbook[SOURCE_SHEET]
        if "Colour code" not in workbook.sheetnames:
            raise ValueError("The verification colour legend is missing.")
        legend = workbook["Colour code"]
        status_by_fill = {
            fill_key(legend[f"C{number}"]): text(legend[f"D{number}"].value)
            for number in (3, 4, 5, 6)
        }
        rows = sheet.iter_rows()
        next(rows, None)  # Workbook title / blank row.
        headers = [text(cell.value) for cell in next(rows)]
        if len(set(headers[:33])) != 33 or EXPECTED - set(headers):
            raise ValueError("Stock Verification headers do not match the reviewed workbook.")
        positions = {name: index for index, name in enumerate(headers) if name}

        candidates = []
        zero_stock_rows = 0
        for row_no, cells in enumerate(rows, start=3):
            raw = {
                header: json_value(cells[index].value if index < len(cells) else None)
                for header, index in positions.items()
            }
            if all(value is None or text(value) == "" for value in raw.values()):
                continue
            available = decimal(raw.get("In Inv"))
            if available is not None and available == 0:
                zero_stock_rows += 1
                continue
            # A nonempty row with an unrecognized stock quantity is a blocker.
            if available is None and not text(raw.get("In Inv")):
                zero_stock_rows += 1
                continue

            errors = []
            warnings = []
            verification = status_by_fill.get(fill_key(cells[5]), "Unlabelled colour")
            if verification.casefold() != "verified":
                errors.append(f"Physical stock verification requires review: {verification}")
            quantity = None
            if available is None or available <= 0 or available != available.to_integral_value():
                errors.append("In Inv must be a positive whole number")
            else:
                quantity = int(available)

            legacy_id = text(raw.get("Comp ID")).upper()
            if legacy_id in MISSING_IDS:
                errors.append("Missing or placeholder Comp ID")
            category = CATEGORY_MAP.get(text(raw.get("Category")).upper(), "")
            if not category:
                errors.append("Category is missing or unsupported")

            price = decimal(raw.get("Unit Price"))
            if price is None or price < 0:
                errors.append("Unit Price is missing or invalid")
            else:
                price = price.quantize(CENT, rounding=ROUND_HALF_UP)
            original_gst = decimal(raw.get("GST %"))
            if original_gst is None and text(raw.get("GST %")):
                errors.append("GST % is invalid")
                rate = None
            else:
                rate = original_gst or Decimal("0")
                if rate < 0 or rate > 100:
                    errors.append("GST % must be between 0 and 100")
                elif rate <= 1:
                    rate *= 100  # Excel records 0.18 for 18%.
                rate = rate.quantize(CENT, rounding=ROUND_HALF_UP)
                if rate not in {Decimal(x) for x in (0, 5, 12, 18, 28)}:
                    warnings.append("Nonstandard GST rate; review source")

            received_date = parse_date(raw.get("Inward Date"), workbook.epoch)
            if received_date is None:
                errors.append("Inward Date is missing or invalid")
            elif received_date > today:
                errors.append("Inward Date is in the future")

            if decimal(raw.get("Shipping")) not in (None, Decimal("0")):
                warnings.append("Original Shipping retained in source metadata, not added to current valuation")
            if decimal(raw.get("Round-off")) not in (None, Decimal("0")):
                warnings.append("Original Round-off retained in source metadata, not added to current valuation")
            hsn = text(raw.get("HSN Code"))
            if hsn and not re.fullmatch(r"[0-9]{4,8}", hsn):
                warnings.append("Invalid HSN retained in source metadata only")

            digest = sha256(json.dumps(
                {"values": raw, "verification": verification},
                sort_keys=True, ensure_ascii=False, allow_nan=False,
            ).encode()).hexdigest()
            candidates.append(StockRow(
                excel_row=row_no,
                source_key=f"stock-verification:{row_no}",
                raw=raw,
                fingerprint=digest,
                legacy_id=legacy_id,
                category=category,
                verification=verification,
                description=text(raw.get("Description")),
                # Recent source rows have a Description but an empty
                # Component Specification; keep that text in stock and master.
                specifications=(text(raw.get("Component Specification"))
                                or text(raw.get("Description"))),
                component_type=text(raw.get("Comp Type")),
                uom=text(raw.get("UOM")),
                quantity=quantity,
                unit_price=price,
                gst_percent=rate,
                received_date=received_date,
                errors=errors,
                warnings=warnings,
            ))

        # A legacy ID maps to a single Component. Even similar descriptions
        # can hide a different size, motor rating or product variant. Hold the
        # entire group until the owner resolves the identity; merging stock
        # under one IPMS component would make later issues unreliable.
        by_id = defaultdict(list)
        for item in candidates:
            if item.legacy_id not in MISSING_IDS:
                by_id[item.legacy_id].append(item)
        for legacy_id, items in by_id.items():
            comparisons = {
                "Category": {item.category for item in items},
                "Description": {normalized_description(item.description) for item in items},
                "Component Specification": {normalized_description(item.specifications) for item in items},
                "Comp Type": {normalized_description(item.component_type) for item in items},
                "UOM": {normalized_description(item.uom) for item in items},
            }
            conflicting = [name for name, values in comparisons.items() if len(values) > 1]
            if conflicting:
                for item in items:
                    item.errors.append(
                        f"Comp ID {legacy_id} has differing {', '.join(conflicting)} across positive-stock rows; "
                        "confirm the correct component ID for each distinct part"
                    )
        return candidates, zero_stock_rows
    finally:
        workbook.close()
