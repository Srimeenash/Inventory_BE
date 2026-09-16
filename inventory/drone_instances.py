import re
from collections import defaultdict

from django.db import transaction

from .models import DroneComponentAllocation, DroneInstance, ProjectInventory


FROM_SCRAP_SERIALS_RE = re.compile(r"FROM_SCRAP_SERIALS:([^\r\n]*)", re.IGNORECASE)


def normalize_serials(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r"[|,;\n]", values)
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    result = []
    seen = set()
    for value in values:
        serial = str(value or "").strip()
        if serial and serial not in seen:
            seen.add(serial)
            result.append(serial)
    return result


def _mr_items(material_request):
    for relation in ("bom_items", "rd_items", "request_items"):
        manager = getattr(material_request, relation, None)
        if manager is None:
            continue
        try:
            rows = list(manager.all())
        except Exception:
            rows = []
        if rows:
            return rows
    return []


def _from_scrap_serials_by_component(material_request):
    result = defaultdict(list)
    for item in _mr_items(material_request):
        component_id = getattr(item, "component_id", None)
        if not component_id:
            continue
        remarks = str(getattr(item, "remarks", "") or "")
        match = FROM_SCRAP_SERIALS_RE.search(remarks)
        if not match:
            continue
        result[int(component_id)].extend(normalize_serials(match.group(1)))
    return {key: normalize_serials(value) for key, value in result.items()}


def _split_quantity(total, instance_count):
    total = max(int(total or 0), 0)
    instance_count = max(int(instance_count or 0), 1)
    base, remainder = divmod(total, instance_count)
    return [base + (1 if index < remainder else 0) for index in range(instance_count)]


def _component_source_payload(project_row, recovered_serials):
    return {
        "from_scrap_serials": normalize_serials(recovered_serials),
        "store_serials": normalize_serials(
            getattr(project_row, "issued_store_serials", []) if project_row else []
        ),
        "purchased_serials": normalize_serials(
            getattr(project_row, "issued_purchased_serials", []) if project_row else []
        ),
    }


def _instance_code(material_request, sequence):
    mr_number = str(material_request.material_request_id or f"MR-{material_request.pk}").strip()
    return f"{mr_number}_{int(sequence):02d}"


@transaction.atomic
def ensure_drone_instances(material_request, project_rows=None, *, lock=False):
    """
    Create one persistent physical DroneInstance per MaterialRequest.required_quantity
    and split the exact issued component serials across those instances.

    Existing allocations are never re-shuffled after creation. This preserves the
    physical serial identity of _01/_02/... for Sale, Returnable and Scrap history.
    """
    if material_request is None:
        return []

    request_type = str(getattr(material_request, "request_type", "") or "").strip().upper()
    if request_type == "RETURNABLE":
        return []

    instance_count = max(int(getattr(material_request, "required_quantity", 0) or 0), 0)
    if instance_count <= 0:
        return []

    queryset = DroneInstance.objects.filter(material_request=material_request).order_by("sequence")
    if lock:
        queryset = queryset.select_for_update()
    existing = {row.sequence: row for row in queryset}

    instances = []
    for sequence in range(1, instance_count + 1):
        instance = existing.get(sequence)
        if instance is None:
            instance = DroneInstance.objects.create(
                material_request=material_request,
                sequence=sequence,
                instance_code=_instance_code(material_request, sequence),
                status="AVAILABLE",
            )
        instances.append(instance)

    if project_rows is None:
        project_rows = list(
            ProjectInventory.objects.select_related("component")
            .filter(material_request=material_request)
            .order_by("component_id", "id")
        )
    else:
        project_rows = list(project_rows)

    # Do not rebuild an already-persisted physical allocation map.
    if DroneComponentAllocation.objects.filter(drone_instance__material_request=material_request).exists():
        return instances

    recovered_by_component = _from_scrap_serials_by_component(material_request)
    item_by_component = {
        int(item.component_id): item
        for item in _mr_items(material_request)
        if getattr(item, "component_id", None)
    }
    project_by_component = {
        int(row.component_id): row
        for row in project_rows
        if getattr(row, "component_id", None)
    }
    component_ids = sorted(set(item_by_component) | set(project_by_component))

    for component_id in component_ids:
        project_row = project_by_component.get(component_id)
        item = item_by_component.get(component_id)
        component = (
            getattr(project_row, "component", None)
            or getattr(item, "component", None)
        )
        if component is None:
            continue

        requested_total = max(
            int(
                getattr(project_row, "requested_quantity", 0)
                or getattr(item, "quantity", 0)
                or 0
            ),
            0,
        )
        quantities = _split_quantity(requested_total, instance_count)

        recovered = recovered_by_component.get(component_id, [])
        store_serials = normalize_serials(
            getattr(project_row, "issued_store_serials", []) if project_row else []
        )
        purchased_serials = normalize_serials(
            getattr(project_row, "issued_purchased_serials", []) if project_row else []
        )
        serials = normalize_serials(recovered + store_serials + purchased_serials)
        source_payload = _component_source_payload(project_row, recovered)

        cursor = 0
        for index, instance in enumerate(instances):
            quantity = quantities[index]
            instance_serials = serials[cursor : cursor + quantity]
            cursor += quantity

            instance_serial_set = set(instance_serials)
            DroneComponentAllocation.objects.create(
                drone_instance=instance,
                component=component,
                quantity=quantity,
                serial_numbers=instance_serials,
                source_details={
                    "from_scrap_serials": [
                        serial for serial in source_payload["from_scrap_serials"]
                        if serial in instance_serial_set
                    ],
                    "store_serials": [
                        serial for serial in source_payload["store_serials"]
                        if serial in instance_serial_set
                    ],
                    "purchased_serials": [
                        serial for serial in source_payload["purchased_serials"]
                        if serial in instance_serial_set
                    ],
                    "allocation_index": index + 1,
                    "requested_total": requested_total,
                    "uom": str(getattr(item, "unit", "") or ""),
                },
            )

    return instances


def _metadata_dict(value):
    return value if isinstance(value, dict) else {}


def _usage_metadata(usage):
    details = getattr(usage, "inventory_issue_details", None)
    if isinstance(details, dict):
        return details
    if isinstance(details, list):
        merged = {}
        for row in details:
            if isinstance(row, dict):
                merged.update(row)
        return merged
    return {}


def _instance_serial_set(instance):
    serials = set()
    for allocation in instance.component_allocations.all():
        serials.update(normalize_serials(allocation.serial_numbers))
    return serials


def _best_instance_for_serials(instances, serials):
    wanted = set(normalize_serials(serials))
    if not wanted:
        return None
    best = None
    best_score = 0
    for instance in instances:
        score = len(_instance_serial_set(instance) & wanted)
        if score > best_score:
            best = instance
            best_score = score
    return best


@transaction.atomic
def refresh_drone_instance_statuses(material_request):
    """Reconcile instance state from persisted Sales, Returnable and Scrap history."""
    instances = list(
        DroneInstance.objects.select_for_update()
        .filter(material_request=material_request)
        .prefetch_related("component_allocations")
        .order_by("sequence")
    )
    if not instances:
        return []

    # Start from AVAILABLE and then apply workflows in increasing precedence.
    state = {instance.pk: ("AVAILABLE", {}, None) for instance in instances}

    def set_state(instance, status, history=None, replacement=None, priority=0):
        if instance is None:
            return
        current = state.get(instance.pk, ("AVAILABLE", {}, None))
        current_priority = int(current[1].get("_priority", 0)) if isinstance(current[1], dict) else 0
        if priority < current_priority:
            return
        history = dict(history or {})
        history["_priority"] = priority
        state[instance.pk] = (status, history, replacement)

    # Returnable / Flight Test / Demo / Event.
    try:
        from componentusage.models import ComponentUsage
        usage_rows = list(
            ComponentUsage.objects.filter(material_request=material_request).order_by("id")
        )
        movement_groups = defaultdict(list)
        for usage in usage_rows:
            metadata = _usage_metadata(usage)
            movement_id = str(metadata.get("movement_id") or f"legacy-{usage.pk}")
            movement_groups[movement_id].append(usage)

        for rows in movement_groups.values():
            first = rows[0]
            metadata = _usage_metadata(first)
            instance = None
            raw_instance_id = metadata.get("drone_instance_id")
            if raw_instance_id:
                instance = next((x for x in instances if str(x.pk) == str(raw_instance_id)), None)
            if instance is None:
                instance = _best_instance_for_serials(
                    instances,
                    [serial for row in rows for serial in normalize_serials(row.issued_serial_numbers)],
                )
            if instance is None:
                continue

            approvals = {str(row.return_approval_status or "").strip().upper() for row in rows}
            conditions = {str(row.return_condition or "").strip().upper() for row in rows}
            received = all(bool(row.received_date) for row in rows)
            purpose = str(first.purpose or "").strip().upper()
            history = {
                "workflow": "RETURNABLE",
                "purpose": purpose,
                "movement_id": metadata.get("movement_id", ""),
                "usage_ids": [row.pk for row in rows],
            }

            if "REJECTED" in approvals and not any(row.received_date for row in rows):
                continue
            if "NOT_OK" in conditions:
                set_state(instance, "QC_FAILED", history, priority=50)
            elif received and conditions == {"OK"} and approvals == {"COMPLETED"}:
                # Successful return releases this same physical drone again.
                set_state(instance, "AVAILABLE", history, priority=40)
            elif received:
                set_state(instance, "RETURN_QC_PENDING", history, priority=30)
            elif "PENDING_MANAGER" in approvals:
                set_state(instance, "RETURNABLE_PENDING", history, priority=25)
            else:
                set_state(instance, "RETURNABLE_ACTIVE", history, priority=30)
    except Exception:
        pass

    # Sales.
    try:
        from outward.models import OutwardEntry
        sales_rows = list(
            OutwardEntry.objects.filter(
                material_request=material_request,
                outward_type="SALES",
            ).order_by("id")
        )
        sales_groups = defaultdict(list)
        for row in sales_rows:
            metadata = _metadata_dict(row.inventory_allocations)
            batch = str(metadata.get("sales_batch_id") or f"legacy-{row.pk}")
            sales_groups[batch].append(row)
        for rows in sales_groups.values():
            metadata = _metadata_dict(rows[0].inventory_allocations)
            instance = None
            if metadata.get("drone_instance_id"):
                instance = next(
                    (x for x in instances if str(x.pk) == str(metadata.get("drone_instance_id"))),
                    None,
                )
            if instance is None:
                instance = _best_instance_for_serials(
                    instances,
                    [serial for row in rows for serial in normalize_serials(row.serial_numbers)],
                )
            if instance is None:
                continue
            statuses = {
                str(row.approval_status or row.status or "").strip().upper()
                for row in rows
            }
            if statuses & {"MANAGEMENT_REJECTED", "REJECTED", "FINANCE_REJECTED"}:
                continue
            history = {
                "workflow": "SALES",
                "sales_row_ids": [row.pk for row in rows],
                "client": rows[0].client or "",
                "invoice_number": rows[0].invoice_number or "",
            }
            if "APPROVED" in statuses:
                set_state(instance, "SOLD", history, priority=80)
            else:
                set_state(instance, "SALE_PENDING", history, priority=70)
    except Exception:
        pass

    # Scrap overrides Returnable/Sales-pending for the affected physical drone.
    try:
        from materialrequest.models import MaterialRequest
        from outward.models import OutwardEntry
        scrap_rows = list(
            OutwardEntry.objects.filter(
                material_request=material_request,
                outward_type="SCRAP",
                scrap_origin="MR",
            ).order_by("id")
        )
        for row in scrap_rows:
            approval = str(row.approval_status or "").strip().upper()
            row_status = str(row.status or "").strip().upper()
            if approval in {"REJECTED", "MANAGER_REJECTED", "FINANCE_REJECTED"} or row_status in {
                "REJECTED", "MANAGER_REJECTED", "FINANCE_REJECTED"
            }:
                continue
            metadata = _metadata_dict(row.inventory_allocations)
            instance = None
            if metadata.get("drone_instance_id"):
                instance = next(
                    (x for x in instances if str(x.pk) == str(metadata.get("drone_instance_id"))),
                    None,
                )
            if instance is None:
                instance = _best_instance_for_serials(instances, row.serial_numbers)
            if instance is None:
                scrap_serials = [
                    serial
                    for item in metadata.get("scrap_items", []) or []
                    if isinstance(item, dict)
                    for serial in normalize_serials(item.get("serial_numbers"))
                ]
                instance = _best_instance_for_serials(instances, scrap_serials)
            if instance is None:
                continue

            reorder_choice = str(
                metadata.get("reorder_choice")
                or metadata.get("manager_disposition_decision")
                or ""
            ).strip().upper()
            replacement = None
            replacement_number = str(metadata.get("replacement_mr_number") or "").strip()
            if replacement_number:
                replacement = MaterialRequest.objects.filter(
                    material_request_id=replacement_number
                ).first()
            history = {
                "workflow": "SCRAP",
                "scrap_id": row.pk,
                "scrap_code": row.code,
                "scrap_mode": metadata.get("scrap_mode", ""),
                "reorder_choice": reorder_choice,
                "replacement_mr_number": replacement_number,
            }
            if approval in {"PENDING_MANAGER", "MANAGER_APPROVED", "PENDING_FINANCE", "REQUESTED"} or row_status in {
                "PENDING_MANAGER", "PENDING_FINANCE"
            }:
                set_state(instance, "SCRAP_PENDING", history, replacement, priority=90)
            elif reorder_choice == "YES":
                set_state(instance, "SCRAPPED_REORDERED", history, replacement, priority=100)
            else:
                set_state(instance, "SCRAPPED", history, replacement, priority=100)
    except Exception:
        pass

    for instance in instances:
        status, history, replacement = state[instance.pk]
        history = dict(history or {})
        history.pop("_priority", None)
        changed = []
        if instance.status != status:
            instance.status = status
            changed.append("status")
        if instance.workflow_metadata != history:
            instance.workflow_metadata = history
            changed.append("workflow_metadata")
        replacement_id = getattr(replacement, "pk", None)
        if instance.replacement_material_request_id != replacement_id:
            instance.replacement_material_request = replacement
            changed.append("replacement_material_request")
        if changed:
            changed.append("updated_at")
            instance.save(update_fields=changed)
    return instances


def instance_status_label(status):
    return {
        "AVAILABLE": "Sale",
        "SALE_PENDING": "Sale Pending",
        "SOLD": "Sold",
        "RETURNABLE_PENDING": "Returnable Pending",
        "RETURNABLE_ACTIVE": "Returnable Active",
        "RETURN_QC_PENDING": "Return QC Pending",
        "QC_FAILED": "QC Failed",
        "SCRAP_PENDING": "Scrap Pending",
        "SCRAPPED": "Scrapped",
        "SCRAPPED_REORDERED": "Scrapped • Reordered",
    }.get(str(status or "").strip().upper(), str(status or "-").replace("_", " ").title())
