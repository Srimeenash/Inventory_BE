"""Public serial formatting helpers for In-Store inventory serial IDs."""

import re

from .models import Inventory


_INTERNAL_SERIAL_RE = re.compile(
    r"^CINV_(?P<inventory_code>[A-Z0-9]+)_S(?P<sequence>\d+)$",
    re.IGNORECASE,
)
_PUBLIC_SERIAL_RE = re.compile(r"^C_\d+S\d+$", re.IGNORECASE)
_CODE_RE = re.compile(r"[^A-Z0-9]+")


def _normalized_code(value):
    return _CODE_RE.sub("", str(value or "").upper())


def get_inventory_code_id_map(context=None):
    """Return a response-cached map from generated stock code to row ID."""
    cache_key = "_public_serial_inventory_code_id_map"
    if isinstance(context, dict) and cache_key in context:
        return context[cache_key]

    code_to_id = {}
    for inventory_id, inventory_code in Inventory.objects.values_list(
        "id", "inventory_code"
    ):
        normalized_code = _normalized_code(inventory_code)
        if normalized_code:
            code_to_id[normalized_code] = inventory_id

    if isinstance(context, dict):
        context[cache_key] = code_to_id
    return code_to_id


def display_serial_number(value, *, inventory_id=None, code_to_id=None):
    """Convert an internal CINV serial to its public C_<row>ID>S<unit> label.

    Serial values already in the public format are kept. If the source stock
    row cannot be resolved, the internal key is not exposed to the UI.
    """
    serial = str(value or "").strip()
    if not serial or _PUBLIC_SERIAL_RE.fullmatch(serial):
        return serial

    match = _INTERNAL_SERIAL_RE.fullmatch(serial)
    if not match:
        return serial

    resolved_inventory_id = inventory_id
    if resolved_inventory_id in (None, ""):
        lookup = code_to_id or {}
        resolved_inventory_id = lookup.get(
            _normalized_code(match.group("inventory_code"))
        )

    try:
        resolved_inventory_id = int(resolved_inventory_id)
        sequence = int(match.group("sequence"))
    except (TypeError, ValueError):
        return "Serial unavailable"

    return f"C_{resolved_inventory_id:05d}S{sequence:05d}"


def display_serial_text(value, *, code_to_id=None):
    """Replace internal serial tokens inside a display-only text string."""
    text = str(value or "")
    pattern = re.compile(
        r"CINV_[A-Z0-9]+_S\d+",
        re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: display_serial_number(
            match.group(0),
            code_to_id=code_to_id,
        ),
        text,
    )


def display_serial_metadata(value, *, code_to_id=None):
    """Copy JSON metadata while replacing serial strings for presentation."""
    if isinstance(value, list):
        return [
            display_serial_metadata(item, code_to_id=code_to_id)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    inventory_id = value.get("inventory_id")
    result = {}
    for key, item in value.items():
        if key in {
            "serial_numbers",
            "issued_serial_numbers",
            "selected_serials",
            "serials",
        } and isinstance(item, list):
            result[key] = [
                display_serial_number(
                    serial,
                    inventory_id=inventory_id,
                    code_to_id=code_to_id,
                )
                for serial in item
            ]
        elif key in {"serial_number", "serialNumber"} and isinstance(item, str):
            result[key] = display_serial_number(
                item,
                inventory_id=inventory_id,
                code_to_id=code_to_id,
            )
        else:
            result[key] = display_serial_metadata(
                item,
                code_to_id=code_to_id,
            )
    return result
