from django.db import transaction
from django.db.models import F, Q, Sum

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from inventory.models import InventoryReservation
from materialrequest.models import MaterialRequest
from notifications.models import Notification

from .models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseRequest,
)
from .serializers import (
    PurchaseOrderSerializer,
    PurchaseRequestSerializer,
)


class PurchaseRequestViewSet(viewsets.ModelViewSet):
    queryset = (
        PurchaseRequest.objects
        .all()
        .order_by("-created_at")
    )
    serializer_class = PurchaseRequestSerializer
    permission_classes = [AllowAny]


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    queryset = (
        PurchaseOrder.objects
        .prefetch_related(
            "items",
            "items__component",
        )
        .all()
        .order_by("-created_at")
    )

    serializer_class = PurchaseOrderSerializer
    permission_classes = [AllowAny]

    # ==========================================================
    # MATERIAL REQUEST / PO WORKFLOW HELPERS
    # ==========================================================

    def get_source_material_request(
        self,
        source_mr_number,
        *,
        lock=False,
    ):
        """
        Resolve the Material Request linked to a Purchase Order.

        source_mr_number normally contains a value such as:
        MR-260804-00002

        A numeric database ID is also accepted as a fallback.
        """

        source_value = str(
            source_mr_number or ""
        ).strip()

        if not source_value:
            return None

        queryset = MaterialRequest.objects

        if lock:
            queryset = queryset.select_for_update()

        lookup = Q(
            material_request_id=source_value
        )

        if source_value.isdigit():
            lookup |= Q(pk=int(source_value))

        return (
            queryset
            .filter(lookup)
            .first()
        )

    def get_material_request_items(
        self,
        material_request,
        *,
        lock=False,
    ):
        """
        Return BOM or R&D component rows for one MR.
        """

        if (
            str(
                material_request.request_type or ""
            ).strip().upper()
            in {"R&D", "RD"}
        ):
            manager = material_request.rd_items
        else:
            manager = material_request.bom_items

        queryset = manager.all()

        if lock:
            queryset = queryset.select_for_update()

        return list(queryset)

    @staticmethod
    def distribute_quantity(items, total_quantity):
        """
        Distribute one component-level quantity across repeated MR rows
        in row order.
        """
        remaining = max(int(total_quantity or 0), 0)
        result = {}

        for item in items:
            required = max(int(item.quantity or 0), 0)
            allocated = min(required, remaining)
            result[item.pk] = allocated
            remaining -= allocated

        return result

    @staticmethod
    def group_material_request_items(request_items):
        groups = {}

        for item in request_items:
            component_id = getattr(item, "component_id", None)

            if not component_id:
                continue

            component_id = int(component_id)

            if component_id not in groups:
                groups[component_id] = {
                    "items": [],
                    "required_quantity": 0,
                }

            groups[component_id]["items"].append(item)
            groups[component_id]["required_quantity"] += max(
                int(item.quantity or 0),
                0,
            )

        return groups

    def get_reservation_shortages(
        self,
        material_request,
        request_items,
        *,
        lock=False,
    ):
        """
        Return the Procurement shortage for each MR component.

        InventoryReservation is the source of truth. The fallback exists
        only for old requests created before the reservation migration.
        """
        queryset = InventoryReservation.objects.filter(
            material_request=material_request
        )

        if lock:
            queryset = queryset.select_for_update()

        reservations = {
            int(row.component_id): row
            for row in queryset
        }

        groups = self.group_material_request_items(request_items)
        result = {}

        for component_id, group in groups.items():
            reservation = reservations.get(component_id)

            if reservation is not None:
                shortage_quantity = max(
                    int(
                        reservation.procurement_shortage_quantity
                        or 0
                    ),
                    0,
                )
                reserved_store_quantity = max(
                    int(
                        reservation.reserved_store_quantity
                        or 0
                    ),
                    0,
                )
            else:
                required_quantity = int(
                    group["required_quantity"] or 0
                )
                reserved_store_quantity = sum(
                    max(
                        int(item.inventory_quantity or 0),
                        0,
                    )
                    for item in group["items"]
                )
                shortage_quantity = max(
                    required_quantity
                    - reserved_store_quantity,
                    0,
                )

            result[component_id] = {
                **group,
                "reserved_store_quantity": (
                    reserved_store_quantity
                ),
                "shortage_quantity": shortage_quantity,
            }

        return result

    def validate_po_against_reserved_shortage(
        self,
        material_request,
    ):
        """
        Prevent active linked POs from ordering more than the shortage
        reserved for Procurement.

        This is called after PO creation while the transaction is still
        open, so a ValidationError rolls the new PO back.
        """
        active_purchase_orders = (
            PurchaseOrder.objects
            .filter(
                source_mr_number=(
                    material_request.material_request_id
                )
            )
            .exclude(
                status__in=[
                    "REJECTED",
                    "FINANCE_REJECTED",
                ]
            )
        )

        ordered_rows = (
            PurchaseOrderItem.objects
            .filter(
                purchase_order__in=active_purchase_orders
            )
            .values("component_id")
            .annotate(
                ordered_quantity=Sum("quantity")
            )
        )

        ordered_by_component = {
            int(row["component_id"]): int(
                row["ordered_quantity"] or 0
            )
            for row in ordered_rows
            if row["component_id"] is not None
        }

        request_items = self.get_material_request_items(
            material_request,
            lock=True,
        )

        shortage_groups = self.get_reservation_shortages(
            material_request,
            request_items,
            lock=True,
        )

        errors = []

        for component_id, ordered_quantity in (
            ordered_by_component.items()
        ):
            allowed_shortage = int(
                shortage_groups
                .get(component_id, {})
                .get("shortage_quantity", 0)
            )

            if ordered_quantity > allowed_shortage:
                errors.append(
                    {
                        "component_id": component_id,
                        "ordered_quantity": ordered_quantity,
                        "allowed_shortage_quantity": (
                            allowed_shortage
                        ),
                    }
                )

        if errors:
            raise ValidationError(
                {
                    "items": [
                        (
                            "PO quantity exceeds the reserved "
                            "Procurement shortage."
                        )
                    ],
                    "components": errors,
                }
            )

    @transaction.atomic
    def sync_material_request_po_progress(
        self,
        source_mr_number,
    ):
        """
        Synchronize one Material Request using every active PO linked to
        that MR.

        The required PO quantity comes from
        InventoryReservation.procurement_shortage_quantity. Therefore,
        stock reserved for an earlier MR cannot be reused by a later MR,
        and Procurement orders only the true remaining shortage.
        """
        material_request = self.get_source_material_request(
            source_mr_number,
            lock=True,
        )

        if not material_request:
            return None

        canonical_mr_number = str(
            material_request.material_request_id
            or source_mr_number
            or ""
        ).strip()

        related_purchase_orders = list(
            PurchaseOrder.objects
            .select_for_update()
            .filter(
                source_mr_number=canonical_mr_number
            )
            .exclude(
                status__in=[
                    "REJECTED",
                    "FINANCE_REJECTED",
                ]
            )
        )

        related_purchase_order_ids = [
            purchase_order.id
            for purchase_order in related_purchase_orders
        ]

        quantity_rows = (
            PurchaseOrderItem.objects
            .filter(
                purchase_order_id__in=(
                    related_purchase_order_ids
                )
            )
            .values("component_id")
            .annotate(
                ordered_quantity=Sum("quantity"),
                delivered_quantity=Sum(
                    "received_quantity"
                ),
            )
        )

        component_progress = {
            int(row["component_id"]): {
                "ordered_quantity": int(
                    row["ordered_quantity"] or 0
                ),
                "delivered_quantity": int(
                    row["delivered_quantity"] or 0
                ),
            }
            for row in quantity_rows
            if row["component_id"] is not None
        }

        request_items = self.get_material_request_items(
            material_request,
            lock=True,
        )

        shortage_groups = self.get_reservation_shortages(
            material_request,
            request_items,
            lock=True,
        )

        shortage_components = []

        for component_id, group in shortage_groups.items():
            progress = component_progress.get(
                component_id,
                {
                    "ordered_quantity": 0,
                    "delivered_quantity": 0,
                },
            )

            ordered_quantity = int(
                progress["ordered_quantity"]
            )
            delivered_quantity = int(
                progress["delivered_quantity"]
            )
            shortage_quantity = int(
                group["shortage_quantity"] or 0
            )

            ordered_distribution = self.distribute_quantity(
                group["items"],
                ordered_quantity,
            )
            delivered_distribution = (
                self.distribute_quantity(
                    group["items"],
                    delivered_quantity,
                )
            )

            for request_item in group["items"]:
                changed_fields = []

                item_ordered = ordered_distribution.get(
                    request_item.pk,
                    0,
                )
                item_delivered = (
                    delivered_distribution.get(
                        request_item.pk,
                        0,
                    )
                )

                if (
                    int(
                        request_item.po_raised_quantity
                        or 0
                    )
                    != item_ordered
                ):
                    request_item.po_raised_quantity = (
                        item_ordered
                    )
                    changed_fields.append(
                        "po_raised_quantity"
                    )

                if (
                    int(
                        request_item.delivered_quantity
                        or 0
                    )
                    != item_delivered
                ):
                    request_item.delivered_quantity = (
                        item_delivered
                    )
                    changed_fields.append(
                        "delivered_quantity"
                    )

                if changed_fields:
                    request_item.save(
                        update_fields=changed_fields
                    )

            if shortage_quantity > 0:
                shortage_components.append(
                    {
                        "component_id": component_id,
                        "shortage_quantity": (
                            shortage_quantity
                        ),
                        "ordered_quantity": (
                            ordered_quantity
                        ),
                        "delivered_quantity": (
                            delivered_quantity
                        ),
                    }
                )

        # An MR with no Procurement shortage belongs only to Inventory.
        if not shortage_components:
            return material_request

        all_shortages_have_po = all(
            row["ordered_quantity"]
            >= row["shortage_quantity"]
            for row in shortage_components
        )

        active_po_exists = bool(
            related_purchase_orders
        )

        all_active_pos_delivered = (
            active_po_exists
            and all(
                str(
                    purchase_order.status or ""
                ).strip().upper()
                == "DELIVERED"
                for purchase_order
                in related_purchase_orders
            )
        )

        all_shortages_delivered = (
            all_shortages_have_po
            and all(
                row["delivered_quantity"]
                >= row["shortage_quantity"]
                for row in shortage_components
            )
        )

        all_delivered = (
            all_active_pos_delivered
            and all_shortages_delivered
        )

        # Professional partial-delivery workflow:
        # the MR must reflect real receipt progress immediately.
        any_shortage_delivered = any(
            int(row["delivered_quantity"] or 0) > 0
            for row in shortage_components
        )

        current_status = str(
            material_request.status or ""
        ).strip().upper()

        later_workflow_statuses = {
            "QC_CHECKED",
            "PROJECT_INVENTORY_READY",
            "INVENTORY_ISSUED",
            "MR_COMPLETED",
        }

        if current_status in later_workflow_statuses:
            return material_request

        reference_id = str(material_request.id)

        if all_delivered:
            material_request.status = "PO_DELIVERED"
            material_request.po_raised = True
            material_request.save(
                update_fields=[
                    "status",
                    "po_raised",
                ]
            )

            Notification.objects.filter(
                category="MR",
                receiver="PROCUREMENT",
                reference_id=reference_id,
            ).update(
                status="PO_DELIVERED",
                is_read=True,
                message=(
                    "All reserved Procurement shortages "
                    "were delivered for "
                    f"{material_request.material_request_id}."
                ),
            )

        elif any_shortage_delivered:
            material_request.status = (
                "PARTIALLY_DELIVERED"
            )
            material_request.po_raised = True
            material_request.save(
                update_fields=[
                    "status",
                    "po_raised",
                ]
            )

            Notification.objects.filter(
                category="MR",
                receiver="PROCUREMENT",
                reference_id=reference_id,
            ).update(
                status="PARTIALLY_DELIVERED",
                is_read=False,
                message=(
                    "Part of the Procurement shortage has "
                    "been delivered for "
                    f"{material_request.material_request_id}. "
                    "Remaining components or quantities are "
                    "still awaiting delivery."
                ),
            )

        elif all_shortages_have_po:
            material_request.status = "PO_RAISED"
            material_request.po_raised = True
            material_request.save(
                update_fields=[
                    "status",
                    "po_raised",
                ]
            )

            Notification.objects.filter(
                category="MR",
                receiver="PROCUREMENT",
                reference_id=reference_id,
            ).update(
                status="PO_RAISED",
                is_read=True,
                message=(
                    "Purchase Orders cover every reserved "
                    "shortage for "
                    f"{material_request.material_request_id}."
                ),
            )

        else:
            material_request.status = (
                "PROCUREMENT_PENDING"
            )
            material_request.po_raised = False
            material_request.save(
                update_fields=[
                    "status",
                    "po_raised",
                ]
            )

            Notification.objects.filter(
                category="MR",
                receiver="PROCUREMENT",
                reference_id=reference_id,
            ).update(
                status="PROCUREMENT_PENDING",
                is_read=False,
                message=(
                    "Additional Purchase Orders are still "
                    "required for the reserved shortage of "
                    f"{material_request.material_request_id}."
                ),
            )

        return material_request

    # ==========================================================
    # PURCHASE ORDER CREATE / UPDATE / DELETE
    # ==========================================================

    @transaction.atomic
    def perform_create(self, serializer):
        """
        Create a PO, reject over-ordering, and synchronize component-wise
        PO progress for the linked Material Request.
        """
        purchase_order = serializer.save()

        if purchase_order.source_mr_number:
            material_request = (
                self.get_source_material_request(
                    purchase_order.source_mr_number,
                    lock=True,
                )
            )

            if material_request:
                self.validate_po_against_reserved_shortage(
                    material_request
                )

            self.sync_material_request_po_progress(
                purchase_order.source_mr_number
            )

    @transaction.atomic
    def perform_update(self, serializer):
        """
        Update the PO, synchronize Finance notification,
        and recalculate the linked MR component progress.
        """

        old_approval_status = str(
            serializer.instance.approval_status
            or ""
        ).upper()

        purchase_order = serializer.save()

        new_approval_status = str(
            purchase_order.approval_status
            or ""
        ).upper()

        # ---------------------------------------------------------
        # Finance approval requested
        # ---------------------------------------------------------
        if (
            old_approval_status
            != "PENDING_FINANCE"
            and new_approval_status
            == "PENDING_FINANCE"
        ):
            Notification.objects.filter(
                category="PO",
                reference_id=purchase_order.id,
                receiver="FINANCE",
            ).delete()

            Notification.objects.create(
                category="PO",
                title=(
                    "PO Approval Request - "
                    f"{purchase_order.po_number}"
                ),
                message=(
                    "Approval requested for PO "
                    f"{purchase_order.po_number}"
                ),
                reference_id=purchase_order.id,
                status="PENDING_FINANCE",
                receiver="FINANCE",
                is_read=False,
            )

            if (
                purchase_order.status
                != "PENDING_FINANCE"
            ):
                purchase_order.status = (
                    "PENDING_FINANCE"
                )
                purchase_order.save(
                    update_fields=["status"]
                )

        # ---------------------------------------------------------
        # Finance approved
        # ---------------------------------------------------------
        elif (
            old_approval_status
            != "FINANCE_APPROVED"
            and new_approval_status
            == "FINANCE_APPROVED"
        ):
            Notification.objects.filter(
                category="PO",
                reference_id=purchase_order.id,
                receiver="FINANCE",
            ).update(
                status="FINANCE_APPROVED",
                is_read=True,
            )

            update_fields = []

            if (
                purchase_order.status
                != "FINANCE_APPROVED"
            ):
                purchase_order.status = (
                    "FINANCE_APPROVED"
                )
                update_fields.append("status")

            if (
                purchase_order.approval_status
                != "FINANCE_APPROVED"
            ):
                purchase_order.approval_status = (
                    "FINANCE_APPROVED"
                )
                update_fields.append(
                    "approval_status"
                )

            if update_fields:
                purchase_order.save(
                    update_fields=update_fields
                )

        # ---------------------------------------------------------
        # Finance rejected
        # ---------------------------------------------------------
        elif (
            old_approval_status
            != "FINANCE_REJECTED"
            and new_approval_status
            == "FINANCE_REJECTED"
        ):
            Notification.objects.filter(
                category="PO",
                reference_id=purchase_order.id,
                receiver="FINANCE",
            ).update(
                status="FINANCE_REJECTED",
                is_read=True,
            )

            update_fields = []

            if (
                purchase_order.status
                != "FINANCE_REJECTED"
            ):
                purchase_order.status = (
                    "FINANCE_REJECTED"
                )
                update_fields.append("status")

            if (
                purchase_order.approval_status
                != "FINANCE_REJECTED"
            ):
                purchase_order.approval_status = (
                    "FINANCE_REJECTED"
                )
                update_fields.append(
                    "approval_status"
                )

            if update_fields:
                purchase_order.save(
                    update_fields=update_fields
                )

        if purchase_order.source_mr_number:
            self.sync_material_request_po_progress(
                purchase_order.source_mr_number
            )

        return purchase_order

    @transaction.atomic
    def perform_destroy(self, instance):
        """
        Recalculate the linked MR when an MR-based PO is deleted.
        """

        source_mr_number = (
            instance.source_mr_number
        )

        instance.delete()

        if source_mr_number:
            self.sync_material_request_po_progress(
                source_mr_number
            )

    # ==========================================================
    # PURCHASE ORDER RECEIPT
    # ==========================================================

    @action(
        detail=True,
        methods=["post"],
        url_path="receive",
    )
    @transaction.atomic
    def receive_purchase_order(
        self,
        request,
        pk=None,
    ):
        """
        Receive full or partial quantities against a PO.

        The server calculates:

        - Remaining quantity exists:
          PARTIALLY_DELIVERED

        - All PO quantities received:
          DELIVERED

        For an MR-based PO, the linked Material Request is then
        recalculated across every PO belonging to the same MR.
        """

        try:
            purchase_order = (
                PurchaseOrder.objects
                .select_for_update()
                .prefetch_related(
                    "items",
                    "items__component",
                )
                .get(pk=pk)
            )
        except PurchaseOrder.DoesNotExist:
            return Response(
                {
                    "detail":
                        "Purchase Order not found."
                },
                status=
                    status.HTTP_404_NOT_FOUND,
            )

        current_status = str(
            purchase_order.status or ""
        ).upper()

        allowed_statuses = {
            "ORDERED",
            "PARTIALLY_DELIVERED",
        }

        if current_status not in allowed_statuses:
            return Response(
                {
                    "detail": (
                        "Only ORDERED or "
                        "PARTIALLY_DELIVERED Purchase "
                        "Orders can receive material."
                    )
                },
                status=
                    status.HTTP_400_BAD_REQUEST,
            )

        received_items = request.data.get(
            "items",
            [],
        )

        if not isinstance(received_items, list):
            return Response(
                {
                    "detail":
                        "The items field must be a list."
                },
                status=
                    status.HTTP_400_BAD_REQUEST,
            )

        if not received_items:
            return Response(
                {
                    "detail":
                        "No received items were provided."
                },
                status=
                    status.HTTP_400_BAD_REQUEST,
            )

        locked_items = (
            purchase_order.items
            .select_for_update()
            .all()
        )

        po_items = {
            str(item.id): item
            for item in locked_items
        }

        received_any_quantity = False

        for received_row in received_items:
            po_item_id = str(
                received_row.get(
                    "po_item_id",
                    "",
                )
            ).strip()

            if not po_item_id:
                return Response(
                    {
                        "detail": (
                            "Every received item must "
                            "include po_item_id."
                        )
                    },
                    status=
                        status.HTTP_400_BAD_REQUEST,
                )

            try:
                quantity_received = int(
                    received_row.get(
                        "quantity_received",
                        0,
                    )
                )
            except (TypeError, ValueError):
                return Response(
                    {
                        "detail": (
                            "Quantity received must be "
                            "a valid whole number."
                        )
                    },
                    status=
                        status.HTTP_400_BAD_REQUEST,
                )

            if quantity_received < 0:
                return Response(
                    {
                        "detail": (
                            "Quantity received cannot "
                            "be negative."
                        )
                    },
                    status=
                        status.HTTP_400_BAD_REQUEST,
                )

            if quantity_received == 0:
                continue

            po_item = po_items.get(po_item_id)

            if not po_item:
                return Response(
                    {
                        "detail": (
                            f"Purchase Order item "
                            f"{po_item_id} does not "
                            "belong to this Purchase "
                            "Order."
                        )
                    },
                    status=
                        status.HTTP_400_BAD_REQUEST,
                )

            ordered_quantity = int(
                po_item.quantity or 0
            )

            previously_received = int(
                po_item.received_quantity or 0
            )

            remaining_quantity = max(
                ordered_quantity
                - previously_received,
                0,
            )

            if remaining_quantity == 0:
                return Response(
                    {
                        "detail": (
                            f"PO item {po_item_id} is "
                            "already fully received."
                        )
                    },
                    status=
                        status.HTTP_400_BAD_REQUEST,
                )

            if (
                quantity_received
                > remaining_quantity
            ):
                return Response(
                    {
                        "detail": (
                            "Received quantity for PO "
                            f"item {po_item_id} cannot "
                            "exceed its remaining "
                            f"quantity of "
                            f"{remaining_quantity}."
                        )
                    },
                    status=
                        status.HTTP_400_BAD_REQUEST,
                )

            po_item.received_quantity = (
                previously_received
                + quantity_received
            )

            po_item.save(
                update_fields=[
                    "received_quantity",
                ]
            )

            received_any_quantity = True

        if not received_any_quantity:
            return Response(
                {
                    "detail": (
                        "Enter at least one received "
                        "quantity greater than zero."
                    )
                },
                status=
                    status.HTTP_400_BAD_REQUEST,
            )

        purchase_order.refresh_from_db()

        has_remaining_quantity = (
            purchase_order.items.filter(
                received_quantity__lt=
                    F("quantity")
            ).exists()
        )

        if has_remaining_quantity:
            purchase_order.status = (
                "PARTIALLY_DELIVERED"
            )
        else:
            purchase_order.status = "DELIVERED"

        purchase_order.save(
            update_fields=["status"]
        )

        # Recalculate every component and every PO in
        # the linked Material Request.
        if purchase_order.source_mr_number:
            self.sync_material_request_po_progress(
                purchase_order.source_mr_number
            )

        purchase_order = (
            PurchaseOrder.objects
            .prefetch_related(
                "items",
                "items__component",
            )
            .get(pk=purchase_order.pk)
        )

        response_serializer = (
            self.get_serializer(
                purchase_order
            )
        )

        return Response(
            response_serializer.data,
            status=status.HTTP_200_OK,
        )