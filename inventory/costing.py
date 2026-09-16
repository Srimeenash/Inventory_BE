"""Persist receipt costs, then look them up by component + serial everywhere.

Read functions never write or fabricate a missing historical value.
"""
from decimal import Decimal
from django.db import transaction
from django.db.models import Prefetch, Q, prefetch_related_objects
from rest_framework.exceptions import ValidationError
from .cost_math import money, allocate, unit_allocations, MONEY_FIELDS
from .models import Inventory, SerialCostAllocation, SerialPurchaseCost


def serials(values):
    """
    Normalize serial values while preserving their original order.

    A set is used only for membership checks, avoiding O(n²) list scans
    when a receipt contains many serial numbers.
    """
    result = []
    seen = set()

    for value in values if isinstance(values, list) else []:
        if isinstance(value, dict):
            value = (
                value.get("serialNumber")
                or value.get("serial_number")
                or value.get("serial")
            )

        value = str(value or "").strip()

        if value and value not in seen:
            seen.add(value)
            result.append(value)

    return result


def totals_for_units(units):
    keys = (*MONEY_FIELDS, 'rounding_adjustment', 'allocated_cost')
    return {key: f'{sum(money(row.get(key)) for row in units):.2f}' for key in keys}


def _po_units(po):
    """
    Build the immutable per-unit PO cost plan.

    Loading optimization:
    - reuse prefetched PO items when available;
    - otherwise prefetch items + Component once and cache them on `po`;
    - this also lets PurchaseOrder.grand_total reuse the same item cache
      when that property reads po.items.
    """
    prefetched_cache = getattr(
        po,
        "_prefetched_objects_cache",
        {},
    )

    if "items" not in prefetched_cache:
        from procurement.models import PurchaseOrderItem

        prefetch_related_objects(
            [po],
            Prefetch(
                "items",
                queryset=(
                    PurchaseOrderItem.objects
                    .select_related("component")
                    .order_by("id")
                ),
            ),
        )

        prefetched_cache = getattr(
            po,
            "_prefetched_objects_cache",
            {},
        )

    items = sorted(
        list(
            prefetched_cache.get(
                "items",
                [],
            )
        ),
        key=lambda item: item.id,
    )

    rounding = (
        money(po.grand_total)
        - sum(
            money(item.total_cost)
            for item in items
        )
    )

    roundoffs = allocate(
        rounding,
        [
            item.total_cost
            for item in items
        ],
    )

    result = {}

    for item, roundoff in zip(
        items,
        roundoffs,
    ):
        totals = dict(
            basic_amount=item.subtotal,
            discount=min(
                money(item.discount),
                money(item.subtotal),
            ),
            gst_amount=item.gst_amount,
            freight_cost=item.freight_cost,
            freight_gst_amount=
                item.freight_gst_amount,
            round_off=roundoff,
            grand_total=(
                money(item.total_cost)
                + roundoff
            ),
        )

        units = unit_allocations(
            totals,
            int(item.quantity),
            unit_price=str(
                item.unit_price
            ),
            gst_percentage=str(
                item.gst_percentage
                or 0
            ),
            freight_gst_percentage=str(
                item.freight_gst_percentage
                or 0
            ),
            po_item_id=item.pk,
            uom=item.uom or "",
        )

        result.setdefault(
            item.component_id,
            [],
        ).extend(units)

    return result



def _ordered_line_items(inward):
    """
    Return Inward line items in ID order without re-querying a relation
    that the ViewSet already prefetched.

    When line_items are not prefetched, fetch them exactly once and keep
    them in Django's normal prefetch cache for the rest of this costing call.
    """
    prefetched_cache = getattr(
        inward,
        "_prefetched_objects_cache",
        {},
    )

    if "line_items" not in prefetched_cache:
        from inward.models import InwardLineItem

        prefetch_related_objects(
            [inward],
            Prefetch(
                "line_items",
                queryset=(
                    InwardLineItem.objects
                    .order_by("id")
                ),
            ),
        )

        prefetched_cache = getattr(
            inward,
            "_prefetched_objects_cache",
            {},
        )

    return sorted(
        list(
            prefetched_cache.get(
                "line_items",
                [],
            )
        ),
        key=lambda item: item.id,
    )
def receipt_plan(inward):
    """
    Allocate only this component's PO share.

    Partial receipts use disjoint slots. Cost formulas and allocation
    rules are unchanged; related rows are simply reused instead of
    queried multiple times.
    """
    po = inward.purchase_order
    count = int(
        inward.quantity_received
        or 0
    )

    if not count:
        raise ValidationError(
            "Received quantity must be positive for costing."
        )

    details = dict(
        inward_id=inward.pk,
        inward_code=inward.code,
        component_id=inward.component_id,
        component_name=
            inward.component.name,
        component_code=
            inward.component.component_id,
        vendor_name=inward.vendor.name,
        po_number=(
            po.po_number
            if po
            else ""
        ),
        mr_number=(
            (po.source_mr_number or "")
            if po
            else ""
        ),
        received_date=str(
            inward.received_date
        ),
        basis=(
            "Purchase Order"
            if po
            else "Inward invoice"
        ),
    )

    line_items = _ordered_line_items(
        inward
    )

    if po:
        from inward.models import InwardEntry
        from django.db.models import Sum

        ordered = _po_units(
            po
        ).get(
            inward.component_id,
            [],
        )

        offset = (
            InwardEntry.objects
            .filter(
                purchase_order_id=po.pk,
                component_id=
                    inward.component_id,
                pk__lt=inward.pk,
            )
            .aggregate(
                n=Sum(
                    "quantity_received"
                )
            )["n"]
            or 0
        )

        units = ordered[
            offset:
            offset + count
        ]

        if len(units) != count:
            raise ValidationError(
                (
                    "Received quantity exceeds the costed PO quantity "
                    "for this component. Check the linked PO and "
                    "earlier receipts."
                )
            )

    else:
        units = []

        for item in line_items:
            quantity = int(
                item.quantity
                or 0
            )

            if (
                quantity <= 0
                or item.unit_price is None
                or item.grand_total is None
            ):
                raise ValidationError(
                    (
                        "Enter invoice quantity, unit price and grand "
                        "total before recording serial purchase costs."
                    )
                )

            basic = money(
                item.unit_price
                * quantity
            )

            gst = money(
                basic
                * Decimal(
                    str(
                        item.gst_percentage
                        or 0
                    )
                )
                / 100
            )

            totals = dict(
                basic_amount=basic,
                gst_amount=gst,
                grand_total=
                    item.grand_total,
                other_charges=(
                    money(
                        item.grand_total
                    )
                    - basic
                    - gst
                ),
            )

            units.extend(
                unit_allocations(
                    totals,
                    quantity,
                    unit_price=str(
                        item.unit_price
                    ),
                    gst_percentage=str(
                        item.gst_percentage
                        or 0
                    ),
                    freight_gst_percentage="0",
                    invoice_number=
                        item.invoice_number
                        or "",
                    invoice_date=str(
                        item.invoice_date
                        or ""
                    ),
                    uom="",
                )
            )

        if len(units) != count:
            raise ValidationError(
                (
                    "Invoice line quantities must equal the received "
                    "quantity before serial costs can be recorded."
                )
            )

    details["invoices"] = [
        dict(
            invoice_number=
                item.invoice_number
                or "",
            invoice_date=str(
                item.invoice_date
                or ""
            ),
            quantity=item.quantity,
            grand_total=(
                str(
                    item.grand_total
                )
                if item.grand_total
                is not None
                else None
            ),
        )
        for item in line_items
    ]

    return units, details


@transaction.atomic
@transaction.atomic
def record_inward_costs(inward):
    """
    Called inside QC's transaction before any serial reaches stock.

    Existing serial detection and allocation-slot detection are loaded in
    one SerialPurchaseCost query instead of two independent queries.
    """
    from inward.models import InwardEntry

    inward = (
        InwardEntry.objects
        .select_for_update()
        .select_related(
            "component",
            "vendor",
            "purchase_order",
        )
        .get(pk=inward.pk)
    )

    rows = (
        list(
            inward.qc_passed_rows
            or []
        )
        + list(
            inward.qc_failed_rows
            or []
        )
    )

    if not rows:
        return None

    # Serialize all receipts of one PO while its cost snapshot is assigned.
    if inward.purchase_order_id:
        from procurement.models import PurchaseOrder

        PurchaseOrder.objects.select_for_update().get(
            pk=inward.purchase_order_id
        )

    allocation = (
        SerialCostAllocation.objects
        .filter(
            source_key=
                f"inward:{inward.pk}"
        )
        .first()
    )

    if allocation is None:
        units, details = receipt_plan(
            inward
        )

        allocation = (
            SerialCostAllocation.objects
            .create(
                source_key=
                    f"inward:{inward.pk}",
                component=
                    inward.component,
                source_inward=inward,
                quantity=len(units),
                source_details=details,
                units=units,
            )
        )

    normalized_serials = serials(
        rows
    )

    relevant_costs = list(
        SerialPurchaseCost.objects
        .filter(
            Q(
                allocation_id=
                    allocation.pk
            )
            | Q(
                component_id=
                    inward.component_id,
                serial_number__in=
                    normalized_serials,
            )
        )
        .select_related(
            "allocation"
        )
    )

    existing = {
        cost.serial_number: cost
        for cost in relevant_costs
        if (
            cost.component_id
            == inward.component_id
            and cost.serial_number
            in normalized_serials
        )
    }

    used = {
        cost.unit_index
        for cost in relevant_costs
        if (
            cost.allocation_id
            == allocation.pk
        )
    }

    for row in rows:
        if int(
            row.get(
                "qty",
                row.get(
                    "quantity",
                    1,
                ),
            )
        ) != 1:
            raise ValidationError(
                (
                    "Each QC serial row must represent exactly "
                    "one component."
                )
            )

        serial = serials(
            [row]
        )

        if not serial:
            raise ValidationError(
                "Each QC row must have a serial number."
            )

        serial = serial[0]
        previous = existing.get(
            serial
        )

        if previous:
            if (
                previous.allocation_id
                != allocation.pk
            ):
                # Actual return flows retain the original cost,
                # never create another value.
                remarks = str(
                    getattr(
                        inward.purchase_order,
                        "remarks",
                        "",
                    )
                    or ""
                ).upper()

                if (
                    "RETURNABLE_RESTORE_OUTWARD:"
                    not in remarks
                ):
                    raise ValidationError(
                        (
                            f"Serial {serial} already belongs to a "
                            "different receipt. Its purchase cost "
                            "cannot be replaced."
                        )
                    )

            continue

        requested_slot = row.get(
            "id"
        )

        try:
            slot = (
                int(
                    requested_slot
                )
                - 1
            )
        except (
            TypeError,
            ValueError,
        ):
            slot = -1

        if (
            slot < 0
            or slot
            >= allocation.quantity
            or slot in used
        ):
            slot = next(
                (
                    index
                    for index in range(
                        allocation.quantity
                    )
                    if index not in used
                ),
                None,
            )

        if slot is None:
            raise ValidationError(
                (
                    "No unassigned purchase-cost slot remains for "
                    "this receipt. Saved serial numbers cannot be "
                    "replaced."
                )
            )

        SerialPurchaseCost.objects.create(
            allocation=allocation,
            component_id=
                inward.component_id,
            serial_number=serial,
            unit_index=slot,
            allocated_cost=
                allocation.units[
                    slot
                ][
                    "allocated_cost"
                ],
        )

        used.add(slot)

    return allocation


@transaction.atomic
def record_inventory_costs(stock, *, allow_legacy=False):
    """Manual stock receives a snapshot; moved/returned serials retain their records."""
    stock = Inventory.objects.select_for_update().select_related('component').get(pk=stock.pk)
    current = serials(stock.serial_numbers)
    all_serials = serials(current + serials(stock.issued_serial_numbers))
    if not all_serials:
        return
    known = set(SerialPurchaseCost.objects.filter(component_id=stock.component_id,
        serial_number__in=all_serials).values_list('serial_number', flat=True))
    missing = [s for s in all_serials if s not in known]
    if not missing:
        update_stock_value(stock)
        return
    # INW-backed and recovered stock must be resolved from their original receipt,
    # never from a changing stock aggregate. Backfill reports these as unpriced.
    if known or stock.purchase_order or str(stock.inventory_code).upper().startswith('INW'):
        return
    if stock.issued_serial_numbers and not allow_legacy:
        return
    allocation, _ = SerialCostAllocation.objects.get_or_create(source_key=f'inventory:{stock.pk}', defaults=dict(
        component_id=stock.component_id, source_inventory=stock, quantity=len(all_serials),
        source_details=dict(component_id=stock.component_id, component_name=stock.component.name,
            component_code=stock.component.component_id, inventory_code=stock.inventory_code,
            vendor_name=stock.vendor or '', po_number=stock.purchase_order or '', mr_number='',
            received_date=str(stock.received_date), basis='Manual opening stock'),
        units=unit_allocations(dict(basic_amount=stock.total_price, grand_total=stock.total_price),len(all_serials),
            unit_price=str(money(stock.total_price) / len(all_serials)), gst_percentage='0', freight_gst_percentage='0',uom='')))
    if len(all_serials) > allocation.quantity:
        raise ValidationError('Add newly purchased stock as a new receipt; this stock cost snapshot is already fixed.')
    for i, serial in enumerate(all_serials):
        if serial in missing:
            SerialPurchaseCost.objects.get_or_create(component_id=stock.component_id, serial_number=serial,
                defaults=dict(allocation=allocation,unit_index=i,allocated_cost=allocation.units[i]['allocated_cost']))
    update_stock_value(stock)


def update_stock_value(stock):
    normalized_serials = serials(
        stock.serial_numbers
    )

    values = list(
        SerialPurchaseCost.objects
        .filter(
            component_id=
                stock.component_id,
            serial_number__in=
                normalized_serials,
        )
        .values_list(
            "allocated_cost",
            flat=True,
        )
    )

    if (
        len(values)
        == len(normalized_serials)
        and len(values)
        == int(
            stock.quantity
            or 0
        )
    ):
        total = sum(
            values,
            Decimal(0),
        )

        Inventory.objects.filter(
            pk=stock.pk
        ).update(
            total_price=total
        )

        stock.total_price = total


def serial_cost_rows(
    component_id,
    numbers,
    statuses=None,
):
    numbers = serials(
        numbers
    )

    if not numbers:
        return []

    costs = {}

    # Keep the IN clause bounded. Large inward receipts can contain tens of
    # thousands of serials, which can exceed MySQL client memory limits when
    # sent as one query.
    chunk_size = 500

    for start in range(
        0,
        len(numbers),
        chunk_size,
    ):
        chunk = numbers[
            start:start + chunk_size
        ]

        chunk_costs = list(
            SerialPurchaseCost.objects
            .filter(
                component_id=component_id,
                serial_number__in=chunk,
            )
            .only(
                "serial_number",
                "allocation_id",
                "unit_index",
                "allocated_cost",
            )
        )

        allocation_ids = {
            cost.allocation_id
            for cost in chunk_costs
        }

        # Load the receipt + PO relationship once so every serial row can
        # expose whether it came from the original PO or a replacement PO.
        allocations = {
            allocation.pk: allocation
            for allocation in (
                SerialCostAllocation.objects
                .filter(
                    pk__in=allocation_ids
                )
                .select_related(
                    "source_inward",
                    "source_inward__purchase_order",
                    "source_inward__purchase_order__replacement_for",
                )
            )
        }

        for cost in chunk_costs:
            allocation = allocations.get(
                cost.allocation_id
            )

            if allocation is not None:
                cost._state.fields_cache[
                    "allocation"
                ] = allocation

            costs[cost.serial_number] = cost

    result = []

    for serial in numbers:
        cost = costs.get(
            serial
        )

        row = dict(
            serial_number=serial,
            status=(
                statuses
                or {}
            ).get(
                serial,
                "",
            ),
            cost_available=bool(
                cost
            ),
        )

        if cost:
            allocation = cost.allocation

            row.update(
                allocation.units[
                    cost.unit_index
                ]
            )

            row.update(
                allocation.source_details
                or {}
            )

            row[
                "allocated_cost"
            ] = str(
                cost.allocated_cost
            )

            # ---------------------------------------------------------
            # Original PO / replacement PO traceability.
            # ---------------------------------------------------------
            source_inward = getattr(
                allocation,
                "source_inward",
                None,
            )

            purchase_order = (
                getattr(
                    source_inward,
                    "purchase_order",
                    None,
                )
                if source_inward
                else None
            )

            if purchase_order:
                row[
                    "po_number"
                ] = str(
                    purchase_order.po_number
                    or ""
                )

                row[
                    "order_type"
                ] = str(
                    getattr(
                        purchase_order,
                        "order_type",
                        "",
                    )
                    or "STANDARD"
                ).upper()

                row[
                    "replacement_round"
                ] = int(
                    getattr(
                        purchase_order,
                        "replacement_round",
                        0,
                    )
                    or 0
                )

                replacement_for = getattr(
                    purchase_order,
                    "replacement_for",
                    None,
                )

                row[
                    "replacement_for_po_number"
                ] = (
                    str(
                        replacement_for.po_number
                        or ""
                    )
                    if replacement_for
                    else ""
                )

        result.append(
            row
        )

    return result

def component_cost_details(
    component_id,
    numbers,
    quantity=None,
    name="",
    statuses=None,
):
    """
    Return component + serial cost details.

    `component_id` remains backward-compatible with DB ID, business
    component ID, and dict inputs. It also accepts an already-loaded
    Component instance so callers with select_related("component") avoid
    another Component query.
    """
    from components.models import Component

    if isinstance(
        component_id,
        Component,
    ):
        component = component_id

    else:
        if isinstance(
            component_id,
            dict,
        ):
            component_id = (
                component_id.get(
                    "id"
                )
                or component_id.get(
                    "component_id"
                )
            )

        if (
            component_id
            and str(
                component_id
            ).isdigit()
        ):
            component = (
                Component.objects
                .only(
                    "id",
                    "component_id",
                    "name",
                )
                .filter(
                    pk=component_id
                )
                .first()
            )

        elif component_id:
            component = (
                Component.objects
                .only(
                    "id",
                    "component_id",
                    "name",
                )
                .filter(
                    component_id=str(
                        component_id
                    )
                )
                .first()
            )

        else:
            component = None

    resolved_component_id = (
        component.pk
        if component
        else None
    )

    normalized_numbers = serials(
        numbers
    )

    rows = (
        serial_cost_rows(
            resolved_component_id,
            normalized_numbers,
            statuses,
        )
        if resolved_component_id
        else [
            dict(
                serial_number=serial,
                cost_available=False,
            )
            for serial
            in normalized_numbers
        ]
    )

    known = [
        row
        for row in rows
        if row.get(
            "cost_available"
        )
    ]

    qty = int(
        quantity
        if quantity is not None
        else len(rows)
    )

    complete = (
        len(known) == qty
        and len(rows) == qty
    )

    return dict(
        component_id=
            resolved_component_id,
        component_name=(
            component.name
            if component
            else name
            or "Component"
        ),
        component_code=(
            component.component_id
            if component
            else ""
        ),
        quantity=qty,
        serials=rows,
        totals=totals_for_units(
            known
        ),
        cost_complete=complete,
        unpriced_quantity=max(
            qty - len(known),
            0,
        ),
    )


def inward_cost_details(inward):
    statuses = {
        serial:
            "QC passed"
        for serial in serials(
            inward.qc_passed_rows
        )
    }

    statuses.update(
        {
            serial:
                "QC failed"
            for serial in serials(
                inward.qc_failed_rows
            )
        }
    )

    component_reference = getattr(
        inward,
        "component",
        None,
    ) or inward.component_id

    detail = component_cost_details(
        component_reference,
        list(statuses),
        inward.quantity_received,
        statuses=statuses,
    )

    allocation = (
        SerialCostAllocation.objects
        .filter(
            source_key=
                f"inward:{inward.pk}"
        )
        .first()
    )

    remarks = str(
        getattr(
            inward.purchase_order,
            "remarks",
            "",
        )
        or ""
    ).upper()

    if (
        "RETURNABLE_RESTORE_OUTWARD:"
        in remarks
    ):
        detail[
            "receipt_source"
        ] = dict(
            vendor_name=
                inward.vendor.name,
            po_number=getattr(
                inward.purchase_order,
                "po_number",
                "",
            ),
            basis=(
                "Original purchase cost "
                "of returned serials"
            ),
        )

        if detail[
            "cost_complete"
        ]:
            detail[
                "receipt_totals"
            ] = detail[
                "totals"
            ]

        detail[
            "pending_costs"
        ] = []

        return [detail]

    try:
        if allocation:
            units = allocation.units
            source = (
                allocation
                .source_details
            )
        else:
            units, source = (
                receipt_plan(
                    inward
                )
            )

        detail[
            "receipt_totals"
        ] = totals_for_units(
            units
        )

        detail[
            "receipt_source"
        ] = source

        assigned = (
            set(
                allocation.serial_costs
                .values_list(
                    "unit_index",
                    flat=True,
                )
            )
            if allocation
            else set()
        )

        detail[
            "pending_costs"
        ] = [
            dict(
                unit_index=index,
                **unit,
            )
            for index, unit
            in enumerate(units)
            if index not in assigned
        ]

    except (
        ValidationError,
        ValueError,
    ) as error:
        detail[
            "cost_error"
        ] = str(
            error.detail
            if isinstance(
                error,
                ValidationError,
            )
            else error
        )

    return [detail]


def outward_cost_details(outward):
    metadata = (
        outward.inventory_allocations
        if isinstance(
            outward.inventory_allocations,
            dict,
        )
        else {}
    )

    groups = (
        metadata.get(
            "scrap_items"
        )
        or metadata.get(
            "scrapItems"
        )
        or metadata.get(
            "failed_items"
        )
        or []
    )

    if groups:
        from components.models import Component

        numeric_ids = set()
        business_ids = set()

        for item in groups:
            reference = (
                item.get(
                    "component_id"
                )
                or item.get(
                    "component"
                )
            )

            if (
                reference
                and str(
                    reference
                ).isdigit()
            ):
                numeric_ids.add(
                    int(reference)
                )
            elif reference:
                business_ids.add(
                    str(reference)
                )

        components_by_pk = {
            component.pk:
                component
            for component in (
                Component.objects
                .only(
                    "id",
                    "component_id",
                    "name",
                )
                .filter(
                    pk__in=numeric_ids
                )
            )
        }

        components_by_code = {
            component.component_id:
                component
            for component in (
                Component.objects
                .only(
                    "id",
                    "component_id",
                    "name",
                )
                .filter(
                    component_id__in=
                        business_ids
                )
            )
        }

        details = []

        for item in groups:
            reference = (
                item.get(
                    "component_id"
                )
                or item.get(
                    "component"
                )
            )

            component = None

            if (
                reference
                and str(
                    reference
                ).isdigit()
            ):
                component = (
                    components_by_pk.get(
                        int(reference)
                    )
                )
            elif reference:
                component = (
                    components_by_code.get(
                        str(reference)
                    )
                )

            details.append(
                component_cost_details(
                    component
                    or reference,
                    (
                        item.get(
                            "serial_numbers"
                        )
                        or item.get(
                            "serialNumbers"
                        )
                        or item.get(
                            "selected_serials"
                        )
                        or []
                    ),
                    item.get(
                        "quantity",
                        item.get(
                            "qty"
                        ),
                    ),
                    item.get(
                        "component_name",
                        "",
                    ),
                )
            )

        return details

    component_reference = getattr(
        outward,
        "component",
        None,
    ) or outward.component_id

    return [
        component_cost_details(
            component_reference,
            outward.serial_numbers,
            outward.quantity,
            outward.product_name,
        )
    ]


def project_cost_details(project):
    issued = (
        serials(
            project.issued_store_serials
        )
        + serials(
            project.issued_purchased_serials
        )
    )

    numbers = serials(
        project.purchased_serial_numbers
        + issued
    )

    issued_set = set(
        issued
    )

    statuses = {
        serial: (
            "Issued"
            if serial in issued_set
            else "Available"
        )
        for serial in numbers
    }

    component_reference = getattr(
        project,
        "component",
        None,
    ) or project.component_id

    return [
        component_cost_details(
            component_reference,
            numbers,
            statuses=statuses,
        )
    ]
