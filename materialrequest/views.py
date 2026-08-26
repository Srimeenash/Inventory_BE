from collections import defaultdict

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q, Sum
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework import status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from inventory.models import (
    Inventory,
    InventoryReservation,
    ProjectInventory,
)
from notifications.email_service import send_ipms_email
from notifications.models import Notification
from procurement.models import (
    PurchaseOrder,
    PurchaseOrderItem,
)
from inward.models import InwardEntry

from .models import MaterialRequest
from .serializers import MaterialRequestSerializer


ACTIVE_RESERVATION_STATUSES = {
    "ACTIVE",
    "PARTIAL",
}

User = get_user_model()


class MaterialRequestViewSet(viewsets.ModelViewSet):
    """
    Material Request workflow.

    Manager approval performs an atomic inventory reservation:

        physical stock
        - unissued reservations belonging to earlier MRs
        = stock available to the current MR

    Existing stock is reserved but is not physically deducted until the
    Inventory team provides the component.
    """


    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]
    queryset = (
        MaterialRequest.objects
        .prefetch_related(
            "bom_items",
            "bom_items__component",
            "rd_items",
            "rd_items__component",
        )
        .all()
        .order_by("-date", "-id")
    )

    serializer_class = MaterialRequestSerializer
    pagination_class = None

    @staticmethod
    def is_from_scrap_request(
        material_request,
    ):
        """
        Detect both NEW and already-created legacy derived Scrap MRs.

        New records:
            request_type = SCRAP

        Legacy records from the previous implementation can still have
        request_type = BOM/R&D but their remarks identify that they were
        automatically recreated from Scrap.
        """
        request_type = str(
            getattr(
                material_request,
                "request_type",
                "",
            )
            or ""
        ).strip().upper()

        if request_type == "SCRAP":
            return True

        remarks = str(
            getattr(
                material_request,
                "remarks",
                "",
            )
            or ""
        ).strip().lower()

        return (
            "automatically recreated from scrap"
            in remarks
            or "automatically created from scrap"
            in remarks
            or "from scrap "
            in remarks
        )


    def get_request_items(self, material_request, *, lock=False):
        request_type = str(
            material_request.request_type or ""
        ).strip().upper()

        manager = (
            material_request.rd_items
            if request_type in {"R&D", "RD"}
            else material_request.bom_items
        )

        queryset = manager.all().order_by("id")

        if lock:
            queryset = queryset.select_for_update()

        return list(queryset)

    def get_component_groups(self, material_request, *, lock=False):
        """
        Group MR rows by component.

        InventoryReservation and ProjectInventory are unique for one
        Material Request + Component, so repeated component rows are
        safely handled as one component requirement.
        """
        groups = defaultdict(
            lambda: {
                "items": [],
                "required_quantity": 0,
                "component": None,
            }
        )

        for item in self.get_request_items(
            material_request,
            lock=lock,
        ):
            component_id = getattr(
                item,
                "component_id",
                None,
            )

            if not component_id:
                continue

            group = groups[component_id]
            group["items"].append(item)
            group["required_quantity"] += max(
                int(item.quantity or 0),
                0,
            )
            group["component"] = getattr(
                item,
                "component",
                None,
            )

        return groups

    @staticmethod
    def get_component_identity(component):
        component_code = (
            getattr(component, "component_id", "")
            or str(component or "")
        )

        component_name = (
            getattr(component, "name", "")
            or str(component or "")
        )

        return component_code, component_name

    @staticmethod
    def get_physical_quantity_from_locked_rows(stock_rows):
        return sum(
            max(int(row.quantity or 0), 0)
            for row in stock_rows
            if not row.issued
        )

    @staticmethod
    def get_other_active_reserved_quantity(
        reservations,
        *,
        material_request_id,
    ):
        total = 0

        for reservation in reservations:
            if (
                reservation.material_request_id
                == material_request_id
            ):
                continue

            if reservation.status not in (
                ACTIVE_RESERVATION_STATUSES
            ):
                continue

            total += max(
                int(
                    reservation.reserved_store_quantity
                    or 0
                )
                - int(
                    reservation.issued_store_quantity
                    or 0
                ),
                0,
            )

        return total

    @staticmethod
    def distribute_quantity(items, total_quantity):
        """
        Allocate a component-level quantity across its MR rows in row
        order. The returned mapping uses each MR item primary key.
        """
        remaining = max(int(total_quantity or 0), 0)
        distribution = {}

        for item in items:
            required = max(int(item.quantity or 0), 0)
            allocated = min(required, remaining)
            distribution[item.pk] = allocated
            remaining -= allocated

        return distribution

    def reserve_request_components(self, material_request):
        """
        Atomically reserve available In-Store stock for every component.

        This method must run inside transaction.atomic().
        """
        groups = self.get_component_groups(
            material_request,
            lock=True,
        )

        if not groups:
            raise ValidationError(
                {
                    "items": [
                        "Material Request has no valid components."
                    ]
                }
            )

        shortages = []
        allocations = []

        # A stable component lock order reduces deadlock risk.
        for component_id in sorted(groups):
            group = groups[component_id]
            items = group["items"]
            component = group["component"]
            required_quantity = int(
                group["required_quantity"] or 0
            )

            # Lock physical stock rows first. Concurrent approvals for
            # the same component will wait here.
            stock_rows = list(
                Inventory.objects
                .select_for_update()
                .filter(
                    component_id=component_id,
                    issued=False,
                    quantity__gt=0,
                )
                .order_by("received_date", "id")
            )

            # Lock all active reservations for the component before
            # calculating availability.
            component_reservations = list(
                InventoryReservation.objects
                .select_for_update()
                .filter(component_id=component_id)
                .order_by("created_at", "id")
            )

            physical_quantity = (
                self.get_physical_quantity_from_locked_rows(
                    stock_rows
                )
            )

            reserved_by_other_mrs = (
                self.get_other_active_reserved_quantity(
                    component_reservations,
                    material_request_id=material_request.id,
                )
            )

            available_for_current_mr = max(
                physical_quantity
                - reserved_by_other_mrs,
                0,
            )

            reserved_store_quantity = min(
                required_quantity,
                available_for_current_mr,
            )

            shortage_quantity = max(
                required_quantity
                - reserved_store_quantity,
                0,
            )

            reservation = next(
                (
                    row
                    for row in component_reservations
                    if row.material_request_id
                    == material_request.id
                ),
                None,
            )

            if reservation is None:
                reservation = (
                    InventoryReservation.objects.create(
                        material_request=material_request,
                        component_id=component_id,
                        requested_quantity=required_quantity,
                        reserved_store_quantity=(
                            reserved_store_quantity
                        ),
                        procurement_shortage_quantity=(
                            shortage_quantity
                        ),
                        issued_store_quantity=0,
                        status="ACTIVE",
                    )
                )
            else:
                issued_store_quantity = min(
                    int(
                        reservation.issued_store_quantity
                        or 0
                    ),
                    reserved_store_quantity,
                )

                reservation.requested_quantity = (
                    required_quantity
                )
                reservation.reserved_store_quantity = (
                    reserved_store_quantity
                )
                reservation.procurement_shortage_quantity = (
                    shortage_quantity
                )
                reservation.issued_store_quantity = (
                    issued_store_quantity
                )

                if reservation.status in {
                    "RELEASED",
                    "CANCELLED",
                }:
                    reservation.status = "ACTIVE"

                reservation.save()

            store_distribution = self.distribute_quantity(
                items,
                reserved_store_quantity,
            )

            # Preserve item.inventory_quantity.
            #
            # The New Material Request page saves the In-Store quantity
            # visible when the MR item is created. Manager approval uses
            # live Inventory and InventoryReservation for routing, but it
            # must not overwrite that creation-time snapshot.
            #
            # Approved Store allocation remains available in
            # InventoryReservation and ProjectInventory.

            project_row, _ = (
                ProjectInventory.objects
                .select_for_update()
                .get_or_create(
                    material_request=material_request,
                    component_id=component_id,
                    defaults={
                        "project": material_request.project,
                        "requested_quantity": (
                            required_quantity
                        ),
                    },
                )
            )

            project_row.project = material_request.project
            project_row.requested_quantity = required_quantity
            project_row.store_quantity = (
                reserved_store_quantity
            )

            # Purchased/QC quantities are preserved if this method is
            # called again after procurement has started.
            project_row.quantity = min(
                required_quantity,
                int(project_row.store_quantity or 0)
                + int(
                    project_row.purchased_quantity or 0
                ),
            )
            project_row.save()

            component_code, component_name = (
                self.get_component_identity(component)
            )

            allocation = {
                "component_id": component_id,
                "component_code": component_code,
                "component_name": component_name,
                "required_quantity": required_quantity,
                "physical_quantity": physical_quantity,
                "reserved_by_other_mrs": (
                    reserved_by_other_mrs
                ),
                "available_quantity": (
                    available_for_current_mr
                ),
                "reserved_store_quantity": (
                    reserved_store_quantity
                ),
                "shortage_quantity": shortage_quantity,
            }

            allocations.append(allocation)

            if shortage_quantity > 0:
                shortages.append(allocation)

        return allocations, shortages

    @staticmethod
    def upsert_notification(
        material_request,
        *,
        receiver,
        title,
        message,
        notification_status,
        is_read=False,
    ):
        """
        Update one notification and remove accidental duplicate rows.
        """
        reference_id = str(material_request.id)

        queryset = Notification.objects.filter(
            category="MR",
            reference_id=reference_id,
            receiver=receiver,
        ).order_by("-id")

        notification = queryset.first()

        if notification is None:
            return Notification.objects.create(
                category="MR",
                title=title,
                message=message,
                reference_id=reference_id,
                status=notification_status,
                receiver=receiver,
                is_read=is_read,
            )

        notification.title = title
        notification.message = message
        notification.status = notification_status
        notification.is_read = is_read
        notification.save(
            update_fields=[
                "title",
                "message",
                "status",
                "is_read",
            ]
        )

        queryset.exclude(pk=notification.pk).delete()
        return notification


    @staticmethod
    def get_user_display_name(user, fallback="User"):
        if user is None:
            return fallback

        return (
            getattr(user, "employee_name", None)
            or getattr(user, "email", None)
            or fallback
        )

    @staticmethod
    def get_ipms_base_url():
        return str(
            getattr(
                settings,
                "IPMS_BASE_URL",
                "http://localhost:5173",
            )
            or "http://localhost:5173"
        ).rstrip("/")

    @staticmethod
    def format_mail_date(value):
        if not value:
            return "-"

        try:
            return value.strftime("%d/%m/%Y")
        except Exception:
            return str(value)

    def send_manager_approval_email(
        self,
        material_request_id,
    ):
        """
        Send the MR approval-request email to every active Manager account.
        This method is called only after the database transaction commits.
        """
        try:
            material_request = (
                MaterialRequest.objects
                .select_related("requester")
                .get(pk=material_request_id)
            )
        except MaterialRequest.DoesNotExist:
            return

        managers = (
            User.objects
            .filter(
                role__iexact="manager",
                is_active=True,
            )
            .exclude(email__isnull=True)
            .exclude(email="")
            .order_by("id")
        )

        requester_name = (
            material_request.requester_name
            or self.get_user_display_name(
                material_request.requester,
                "User",
            )
        )

        subject = (
            f"{material_request.material_request_id} "
            f"submitted by {requester_name} "
            f"- Approval Required"
        )

        action_url = (
            f"{self.get_ipms_base_url()}/notifications"
        )

        for manager in managers:
            send_ipms_email(
                recipient_email=manager.email,
                subject=subject,
                context={
                    "recipient_name": (
                        self.get_user_display_name(
                            manager,
                            "Manager",
                        )
                    ),
                    "message": (
                        f"The following Material Request "
                        f"submitted by {requester_name} "
                        f"is awaiting your approval."
                    ),
                    "table_headers": [
                        "MR ID",
                        "Request Type",
                        "Project",
                        "Submitted By",
                        "Required Date",
                        "Status",
                    ],
                    "table_values": [
                        material_request.material_request_id,
                        material_request.request_type,
                        material_request.project,
                        requester_name,
                        self.format_mail_date(
                            material_request.required_date
                        ),
                        "Pending Manager",
                    ],
                    "status": "Pending Manager",
                    "instruction": (
                        "Please review the request and take "
                        "the appropriate action in IPMS."
                    ),
                    "button_text": (
                        "Review Request in IPMS"
                    ),
                    "action_url": action_url,
                },
            )

    def send_requester_result_email(
        self,
        material_request_id,
        *,
        outcome,
        action_role="Manager",
    ):
        """
        Email the original MR requester after Manager approval/rejection.
        """
        try:
            material_request = (
                MaterialRequest.objects
                .select_related("requester")
                .get(pk=material_request_id)
            )
        except MaterialRequest.DoesNotExist:
            return

        requester = material_request.requester

        if (
            requester is None
            or not getattr(requester, "email", None)
        ):
            # Existing old MRs may have requester_name only.
            # Do not guess an email address from a free-text name.
            return

        requester_name = (
            material_request.requester_name
            or self.get_user_display_name(
                requester,
                "User",
            )
        )

        normalized_outcome = str(
            outcome or ""
        ).strip().lower()

        approved = normalized_outcome == "approved"

        if approved:
            subject = (
                f"{material_request.material_request_id} "
                f"has been approved by {action_role}"
            )

            message = (
                f"Your Material Request "
                f"{material_request.material_request_id} "
                f"has been approved by {action_role}."
            )

            mail_status = "Approved"

            instruction = (
                "Your request has moved to the next "
                "stage of the IPMS workflow."
            )

            table_headers = [
                "MR ID",
                "Request Type",
                "Project",
                "Submitted By",
                "Approved By",
                "Status",
            ]

            table_values = [
                material_request.material_request_id,
                material_request.request_type,
                material_request.project,
                requester_name,
                action_role,
                mail_status,
            ]

        else:
            subject = (
                f"{material_request.material_request_id} "
                f"has been rejected by {action_role}"
            )

            message = (
                f"Your Material Request "
                f"{material_request.material_request_id} "
                f"has been rejected by {action_role}."
            )

            mail_status = "Rejected"

            instruction = (
                "Please review the rejection reason "
                "and make the required correction."
            )

            table_headers = [
                "MR ID",
                "Request Type",
                "Project",
                "Submitted By",
                "Rejected By",
                "Status",
                "Rejection Reason",
            ]

            table_values = [
                material_request.material_request_id,
                material_request.request_type,
                material_request.project,
                requester_name,
                (
                    material_request.rejected_by
                    or action_role
                ),
                mail_status,
                (
                    material_request.rejection_reason
                    or "-"
                ),
            ]

        send_ipms_email(
            recipient_email=requester.email,
            subject=subject,
            context={
                "recipient_name": requester_name,
                "message": message,
                "table_headers": table_headers,
                "table_values": table_values,
                "status": mail_status,
                "instruction": instruction,
                "button_text": "View Request in IPMS",
                "action_url": (
                    f"{self.get_ipms_base_url()}"
                    f"/material-requests"
                ),
            },
        )

    def send_procurement_required_email(
        self,
        material_request_id,
    ):
        """
        Email every active Procurement user after Manager approval when the
        Material Request still has an unreserved shortage that requires a PO.
        """
        try:
            material_request = (
                MaterialRequest.objects
                .select_related("requester")
                .get(pk=material_request_id)
            )
        except MaterialRequest.DoesNotExist:
            return

        reservations = list(
            InventoryReservation.objects
            .select_related("component")
            .filter(
                material_request=material_request,
                procurement_shortage_quantity__gt=0,
            )
            .order_by("id")
        )

        if not reservations:
            return

        procurement_users = (
            User.objects
            .filter(
                role__iexact="procurement",
                is_active=True,
            )
            .exclude(email__isnull=True)
            .exclude(email="")
            .order_by("id")
        )

        if not procurement_users.exists():
            return

        requester_name = (
            material_request.requester_name
            or self.get_user_display_name(
                material_request.requester,
                "User",
            )
        )

        total_shortage = sum(
            max(
                int(
                    reservation.procurement_shortage_quantity
                    or 0
                ),
                0,
            )
            for reservation in reservations
        )

        component_lines = []

        for reservation in reservations:
            component_code, component_name = (
                self.get_component_identity(
                    reservation.component
                )
            )

            component_lines.append(
                (
                    f"{component_code} "
                    f"{component_name} - "
                    f"Purchase: "
                    f"{int(reservation.procurement_shortage_quantity or 0)}"
                ).strip()
            )

        component_summary = "; ".join(
            component_lines
        )

        subject = (
            f"{material_request.material_request_id} "
            f"approved - Procurement Action Required"
        )

        action_url = (
            f"{self.get_ipms_base_url()}"
            f"/materialsnotifications"
        )

        for procurement_user in procurement_users:
            send_ipms_email(
                recipient_email=procurement_user.email,
                subject=subject,
                context={
                    "recipient_name": (
                        self.get_user_display_name(
                            procurement_user,
                            "Procurement",
                        )
                    ),
                    "message": (
                        f"Material Request "
                        f"{material_request.material_request_id} "
                        f"has been approved by Manager and "
                        f"requires procurement."
                    ),
                    "table_headers": [
                        "MR ID",
                        "Project",
                        "Requested By",
                        "Purchase Qty",
                        "Components",
                        "Status",
                    ],
                    "table_values": [
                        material_request.material_request_id,
                        material_request.project,
                        requester_name,
                        total_shortage,
                        component_summary,
                        "Procurement Pending",
                    ],
                    "status": "Procurement Pending",
                    "instruction": (
                        "Please review the shortage and raise "
                        "the required Purchase Order in IPMS."
                    ),
                    "button_text": (
                        "Open Procurement in IPMS"
                    ),
                    "action_url": action_url,
                },
            )



    def send_inventory_required_email(
        self,
        material_request_id,
    ):
        """
        Email every active Inventory user after Manager approval when
        existing In-Store stock has been reserved for this MR.

        This intentionally works alongside Procurement:
        a partially available MR can send BOTH Inventory and Procurement
        emails from the same Manager approval.
        """
        try:
            material_request = (
                MaterialRequest.objects
                .select_related("requester")
                .get(pk=material_request_id)
            )
        except MaterialRequest.DoesNotExist:
            return False

        reservations = list(
            InventoryReservation.objects
            .select_related("component")
            .filter(
                material_request=material_request,
                reserved_store_quantity__gt=0,
            )
            .order_by("id")
        )

        reservation_rows = []

        for reservation in reservations:
            reserved = max(
                int(
                    reservation.reserved_store_quantity
                    or 0
                ),
                0,
            )

            issued = max(
                int(
                    reservation.issued_store_quantity
                    or 0
                ),
                0,
            )

            remaining = max(
                reserved - issued,
                0,
            )

            if remaining <= 0:
                continue

            reservation_rows.append(
                (
                    reservation,
                    remaining,
                )
            )

        if not reservation_rows:
            return False

        inventory_users = (
            User.objects
            .filter(
                role__iexact="inventory",
                is_active=True,
            )
            .exclude(email__isnull=True)
            .exclude(email="")
            .order_by("id")
        )

        if not inventory_users.exists():
            return False

        requester_name = (
            material_request.requester_name
            or self.get_user_display_name(
                material_request.requester,
                "User",
            )
        )

        total_ready = sum(
            remaining
            for _, remaining
            in reservation_rows
        )

        component_lines = []

        for reservation, remaining in reservation_rows:
            component_code, component_name = (
                self.get_component_identity(
                    reservation.component
                )
            )

            component_lines.append(
                (
                    f"{component_code} "
                    f"{component_name} - "
                    f"Ready to Issue: {remaining}"
                ).strip()
            )

        component_summary = "; ".join(
            component_lines
        )

        subject = (
            f"{material_request.material_request_id} "
            f"approved - Inventory Action Required"
        )

        action_url = (
            f"{self.get_ipms_base_url()}"
            f"/notifications"
        )

        sent_any = False

        for inventory_user in inventory_users:
            sent = send_ipms_email(
                recipient_email=inventory_user.email,
                subject=subject,
                context={
                    "recipient_name": (
                        self.get_user_display_name(
                            inventory_user,
                            "Inventory",
                        )
                    ),
                    "message": (
                        f"Material Request "
                        f"{material_request.material_request_id} "
                        f"has been approved by Manager. "
                        f"Existing In-Store stock is reserved "
                        f"and ready to issue."
                    ),
                    "table_headers": [
                        "MR ID",
                        "Project",
                        "Requested By",
                        "Ready Qty",
                        "Components",
                        "Status",
                    ],
                    "table_values": [
                        material_request.material_request_id,
                        material_request.project,
                        requester_name,
                        total_ready,
                        component_summary,
                        "Inventory Pending",
                    ],
                    "status": "Inventory Pending",
                    "instruction": (
                        "Please review the reserved components "
                        "and provide the available quantities "
                        "to this Material Request in IPMS."
                    ),
                    "button_text": (
                        "Open Inventory Request in IPMS"
                    ),
                    "action_url": action_url,
                },
            )

            if sent:
                sent_any = True

        return sent_any


    def create_manager_notification(self, material_request):
        self.upsert_notification(
            material_request,
            receiver="MANAGER",
            title=(
                "MR Approval Request - "
                f"{material_request.material_request_id}"
            ),
            message=(
                f"Material Request "
                f"{material_request.material_request_id} "
                f"requires manager approval."
            ),
            notification_status="PENDING_MANAGER",
            is_read=False,
        )

    def route_after_manager_approval(
        self,
        material_request,
    ):
        """
        Reserve existing stock and route the Material Request source-wise.

        IMPORTANT SPLIT FLOW:
        - Any quantity reserved from existing In-Store stock is immediately
          exposed to INVENTORY for issuing.
        - Only the remaining shortage is exposed to PROCUREMENT for PO raising.
        - Both notifications may exist at the same time for the same MR.

        Example:
            Requested Wings = 10
            Reserved In Store = 2
            Procurement shortage = 8

        Result:
            INVENTORY notification -> issue 2 Wings
            PROCUREMENT notification -> raise PO for 8 Wings
        """
        # ------------------------------------------------------
        # FROM-SCRAP MR
        # ------------------------------------------------------
        # These components already physically come from the scrapped drone.
        # They must NOT be reserved from central In Store, must NOT enter
        # Project Inventory, and must NOT create Procurement work.
        #
        # Manager approval therefore completes this derived MR directly.
        # ------------------------------------------------------
        if self.is_from_scrap_request(
            material_request
        ):
            reference_id = str(
                material_request.id
            )

            Notification.objects.filter(
                category="MR",
                reference_id=reference_id,
                receiver="MANAGER",
            ).update(
                status="MANAGER_APPROVED",
                is_read=True,
            )

            # Defensive cleanup in case an old deployment created work.
            Notification.objects.filter(
                category="MR",
                reference_id=reference_id,
                receiver__in=[
                    "INVENTORY",
                    "PROCUREMENT",
                ],
            ).delete()

            # There should be no active Inventory reservation for a
            # From-Scrap MR. Release/delete any stale rows defensively.
            InventoryReservation.objects.filter(
                material_request=
                    material_request
            ).delete()

            # No ProjectInventory row should be needed for this MR.
            ProjectInventory.objects.filter(
                material_request=
                    material_request
            ).delete()

            material_request.approval_status = (
                "MANAGER_APPROVED"
            )
            material_request.status = (
                "MR_COMPLETED"
            )
            material_request.po_raised = False

            material_request.save(
                update_fields=[
                    "status",
                    "approval_status",
                    "po_raised",
                ]
            )

            return

        allocations, shortages = (
            self.reserve_request_components(
                material_request
            )
        )

        reference_id = str(material_request.id)

        Notification.objects.filter(
            category="MR",
            reference_id=reference_id,
            receiver="MANAGER",
        ).update(
            status="MANAGER_APPROVED",
            is_read=True,
        )

        material_request.approval_status = (
            "MANAGER_APPROVED"
        )

        # ----------------------------------------------------------
        # Source-wise split.
        # A component can be BOTH:
        #   reserved partially from In Store
        #   AND short for Procurement.
        # ----------------------------------------------------------
        store_allocations = [
            item
            for item in allocations
            if int(
                item.get(
                    "reserved_store_quantity",
                    0,
                )
                or 0
            ) > 0
        ]

        # ----------------------------------------------------------
        # Overall MR status remains PROCUREMENT_PENDING while even
        # one procurement shortage exists. This does NOT prevent the
        # separate INVENTORY notification from being actionable.
        # ----------------------------------------------------------
        if shortages:
            material_request.status = (
                "PROCUREMENT_PENDING"
            )
        else:
            material_request.status = (
                "INVENTORY_PENDING"
            )

        material_request.save(
            update_fields=[
                "status",
                "approval_status",
            ]
        )

        # ----------------------------------------------------------
        # INVENTORY ROUTE
        #
        # Do NOT delete this notification just because Procurement is
        # also required. Inventory must be able to issue the reserved
        # quantity immediately.
        # ----------------------------------------------------------
        if store_allocations:
            allocation_text = "; ".join(
                (
                    f"{item['component_code']} "
                    f"{item['component_name']} - "
                    f"Reserved from In Store: "
                    f"{item['reserved_store_quantity']}"
                )
                for item in store_allocations
            )

            total_store_ready = sum(
                int(
                    item.get(
                        "reserved_store_quantity",
                        0,
                    )
                    or 0
                )
                for item in store_allocations
            )

            self.upsert_notification(
                material_request,
                receiver="INVENTORY",
                title=(
                    "Inventory Issue Request - "
                    f"{material_request.material_request_id}"
                ),
                message=(
                    f"Manager approved "
                    f"{material_request.material_request_id}. "
                    f"{total_store_ready} unit(s) are reserved "
                    f"and ready to issue from In Store: "
                    f"{allocation_text}"
                ),
                notification_status=(
                    "INVENTORY_PENDING"
                ),
                is_read=False,
            )
        else:
            # No physical stock is reserved for this MR.
            Notification.objects.filter(
                category="MR",
                reference_id=reference_id,
                receiver="INVENTORY",
            ).delete()

        # ----------------------------------------------------------
        # PROCUREMENT ROUTE
        #
        # Send ONLY the unreserved shortage. Never ask Procurement to
        # purchase the quantity already reserved from In Store.
        # ----------------------------------------------------------
        if shortages:
            shortage_text = "; ".join(
                (
                    f"{item['component_code']} "
                    f"{item['component_name']} - "
                    f"Requested: "
                    f"{item['required_quantity']}, "
                    f"Reserved from In Store: "
                    f"{item['reserved_store_quantity']}, "
                    f"Purchase: "
                    f"{item['shortage_quantity']}"
                )
                for item in shortages
            )

            total_shortage = sum(
                int(
                    item.get(
                        "shortage_quantity",
                        0,
                    )
                    or 0
                )
                for item in shortages
            )

            self.upsert_notification(
                material_request,
                receiver="PROCUREMENT",
                title=(
                    "Procurement Required - "
                    f"{material_request.material_request_id}"
                ),
                message=(
                    f"Manager approved "
                    f"{material_request.material_request_id}. "
                    f"Raise Purchase Order only for the "
                    f"{total_shortage} unit(s) still short: "
                    f"{shortage_text}"
                ),
                notification_status=(
                    "PROCUREMENT_PENDING"
                ),
                is_read=False,
            )
        else:
            # Everything required by the MR is covered by reserved
            # In-Store stock; Procurement has nothing to purchase.
            Notification.objects.filter(
                category="MR",
                reference_id=reference_id,
                receiver="PROCUREMENT",
            ).delete()

    @staticmethod
    def release_reservations(material_request):
        """
        Release all unissued stock when an MR is rejected or cancelled.
        """
        reservations = (
            InventoryReservation.objects
            .select_for_update()
            .filter(material_request=material_request)
        )

        for reservation in reservations:
            if reservation.status in {
                "RELEASED",
                "CANCELLED",
            }:
                continue

            reservation.status = "RELEASED"
            reservation.save(update_fields=["status"])

            project_row = (
                ProjectInventory.objects
                .select_for_update()
                .filter(
                    material_request=material_request,
                    component_id=(
                        reservation.component_id
                    ),
                )
                .first()
            )

            if project_row:
                # Keep only the quantity already physically issued.
                project_row.store_quantity = int(
                    project_row.issued_store_quantity
                    or 0
                )
                project_row.quantity = min(
                    int(
                        project_row.requested_quantity
                        or 0
                    ),
                    int(project_row.store_quantity or 0)
                    + int(
                        project_row.purchased_quantity
                        or 0
                    ),
                )
                project_row.save()

    @transaction.atomic
    def perform_create(self, serializer):
        current_user = self.request.user

        requester_name = (
            getattr(
                current_user,
                "employee_name",
                None,
            )
            or getattr(
                current_user,
                "email",
                None,
            )
            or "User"
        )

        material_request = serializer.save(
            requester=current_user,
            requester_name=requester_name,
            status="PENDING_MANAGER",
            approval_status="PENDING_MANAGER",
        )

        self.create_manager_notification(
            material_request
        )

        transaction.on_commit(
            lambda mr_id=material_request.id: (
                self.send_manager_approval_email(
                    mr_id
                )
            )
        )

    @transaction.atomic
    def perform_update(self, serializer):
        old_instance = self.get_object()

        old_approval_status = str(
            old_instance.approval_status or ""
        ).strip().upper()

        old_status = str(
            old_instance.status or ""
        ).strip().upper()

        material_request = serializer.save()

        new_approval_status = str(
            material_request.approval_status or ""
        ).strip().upper()

        new_status = str(
            material_request.status or ""
        ).strip().upper()

        downstream_statuses = {
            "PARTIALLY_DELIVERED",
            "PO_DELIVERED",
            "QC_CHECKED",
            "PROJECT_INVENTORY_READY",
            "INVENTORY_ISSUED",
            "MR_COMPLETED",
        }

        if (
            new_status == "PO_RAISED"
            and old_status not in downstream_statuses
        ):
            material_request.po_raised = True
            material_request.status = "PO_RAISED"
            material_request.save(
                update_fields=[
                    "po_raised",
                    "status",
                ]
            )

            Notification.objects.filter(
                category="MR",
                reference_id=str(material_request.id),
                receiver="PROCUREMENT",
            ).update(
                status="PO_RAISED",
                is_read=True,
                message=(
                    f"Purchase Orders raised for "
                    f"{material_request.material_request_id}."
                ),
            )
            return

        if (
            new_approval_status == "PENDING_MANAGER"
            and old_approval_status
            != "PENDING_MANAGER"
        ):
            material_request.status = (
                "PENDING_MANAGER"
            )

            material_request.save(
                update_fields=[
                    "status",
                    "approval_status",
                ]
            )

            self.create_manager_notification(
                material_request
            )

            # Re-send approval email when MR is
            # submitted again to Manager.
            transaction.on_commit(
                lambda mr_id=material_request.id: (
                    self.send_manager_approval_email(
                        mr_id
                    )
                )
            )

            return

        if (
            new_approval_status == "MANAGER_APPROVED"
            and old_approval_status
            != "MANAGER_APPROVED"
            and new_status not in {
                "PROCUREMENT_PENDING",
                "INVENTORY_PENDING",
                "PO_RAISED",
                "PARTIALLY_DELIVERED",
                "PO_DELIVERED",
                "QC_CHECKED",
                "PROJECT_INVENTORY_READY",
                "INVENTORY_ISSUED",
                "MR_COMPLETED",
            }
        ):
            self.route_after_manager_approval(
                material_request
            )

            transaction.on_commit(
                lambda mr_id=material_request.id: (
                    self.send_requester_result_email(
                        mr_id,
                        outcome="approved",
                        action_role="Manager",
                    )
                )
            )

            if (
                str(
                    material_request.status or ""
                ).strip().upper()
                == "PROCUREMENT_PENDING"
            ):
                transaction.on_commit(
                    lambda mr_id=material_request.id: (
                        self.send_procurement_required_email(
                            mr_id
                        )
                    )
                )

            # Inventory and Procurement are independent source-wise routes.
            # If any existing stock is reserved, Inventory must receive an
            # email even when this same MR also has a Procurement shortage.
            has_inventory_allocation = (
                InventoryReservation.objects
                .filter(
                    material_request=material_request,
                    reserved_store_quantity__gt=0,
                )
                .exists()
            )

            if has_inventory_allocation:
                transaction.on_commit(
                    lambda mr_id=material_request.id: (
                        self.send_inventory_required_email(
                            mr_id
                        )
                    )
                )

            return

        if (
            new_approval_status == "MANAGER_REJECTED"
            or new_status in {
                "MANAGER_REJECTED",
                "REJECTED",
            }
        ):
            self.release_reservations(
                material_request
            )

            material_request.status = (
                "MANAGER_REJECTED"
            )
            material_request.approval_status = (
                "MANAGER_REJECTED"
            )
            material_request.save(
                update_fields=[
                    "status",
                    "approval_status",
                    "rejection_reason",
                    "rejected_by",
                ]
            )

            reference_id = str(material_request.id)

            Notification.objects.filter(
                category="MR",
                reference_id=reference_id,
                receiver="MANAGER",
            ).update(
                status="MANAGER_REJECTED",
                is_read=True,
            )

            Notification.objects.filter(
                category="MR",
                reference_id=reference_id,
                receiver__in=[
                    "PROCUREMENT",
                    "INVENTORY",
                ],
            ).delete()

            transaction.on_commit(
                lambda mr_id=material_request.id: (
                    self.send_requester_result_email(
                        mr_id,
                        outcome="rejected",
                        action_role="Manager",
                    )
                )
            )
            return

    @transaction.atomic
    def destroy(
        self,
        request,
        *args,
        **kwargs,
    ):
        """
        Permanently delete one Material Request and its complete
        MR-linked workflow.

        Deleted together:
        - MR BOM / R&D component rows
        - InventoryReservation rows
        - ProjectInventory rows
        - Purchase Orders whose source_mr_number is this MR
        - PurchaseOrderItem rows belonging to those POs
        - Inward entries belonging to those POs
        - Inward line items and saved QC pass/fail rows
        - MR, Procurement, Inventory and Finance notifications

        Not deleted:
        - Direct Purchase Orders without this source_mr_number
        - Direct Inward entries without this MR link
        - unrelated central In-Store Inventory
        """
        try:
            material_request = (
                MaterialRequest.objects
                .select_for_update()
                .get(pk=kwargs.get("pk"))
            )
        except MaterialRequest.DoesNotExist:
            return Response(
                {
                    "detail": (
                        "Material Request was not found."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        deleted_id = material_request.id

        mr_number = str(
            material_request.material_request_id
            or ""
        ).strip()

        # ------------------------------------------------------
        # 1. Find every PO created from this exact MR.
        # Direct POs have no matching source_mr_number and are
        # therefore never included.
        # ------------------------------------------------------
        linked_purchase_orders = list(
            PurchaseOrder.objects
            .select_for_update()
            .filter(
                source_mr_number=mr_number
            )
            .order_by("id")
        )

        linked_po_ids = [
            purchase_order.id
            for purchase_order
            in linked_purchase_orders
        ]

        linked_po_numbers = [
            str(
                getattr(
                    purchase_order,
                    "po_number",
                    "",
                )
                or purchase_order.id
            )
            for purchase_order
            in linked_purchase_orders
        ]

        # ------------------------------------------------------
        # 2. Lock all Inward records created from the MR POs.
        # QC pass/fail rows are stored on these Inward entries.
        # ------------------------------------------------------
        linked_inwards = []

        if linked_po_ids:
            linked_inwards = list(
                InwardEntry.objects
                .select_for_update()
                .filter(
                    purchase_order_id__in=(
                        linked_po_ids
                    )
                )
                .order_by("id")
            )

        linked_inward_ids = [
            inward.id
            for inward in linked_inwards
        ]

        linked_inward_codes = [
            str(inward.code).strip()
            for inward in linked_inwards
            if str(inward.code or "").strip()
        ]

        # ------------------------------------------------------
        # 3. Delete accidental Inventory rows created by old code
        # for an MR-linked Inward. Correct current code stores this
        # stock only in ProjectInventory, but this cleanup prevents
        # old test data from remaining as free In-Store stock.
        #
        # Inventory rows belonging to Direct Inward are unaffected,
        # because only codes from this MR's linked Inward entries
        # are used.
        # ------------------------------------------------------
        deleted_inventory_rows = 0

        if linked_inward_codes:
            deleted_inventory_rows, _ = (
                Inventory.objects
                .select_for_update()
                .filter(
                    inventory_code__in=(
                        linked_inward_codes
                    )
                )
                .delete()
            )

        # ------------------------------------------------------
        # 4. Delete Inward records first.
        # InwardLineItem rows are removed by their FK cascade.
        # This also removes stored QC pass/fail JSON with the entry.
        # ------------------------------------------------------
        deleted_inward_rows = 0

        if linked_inward_ids:
            deleted_inward_rows, _ = (
                InwardEntry.objects
                .filter(
                    id__in=linked_inward_ids
                )
                .delete()
            )

        # ------------------------------------------------------
        # 5. Delete all PO-related notifications, including Finance
        # approval notifications whose reference_id is a PO ID.
        # ------------------------------------------------------
        po_reference_values = [
            str(po_id)
            for po_id in linked_po_ids
        ]

        po_notification_filter = Q()

        if po_reference_values:
            po_notification_filter |= Q(
                category="PO",
                reference_id__in=(
                    po_reference_values
                ),
            )

        for po_number in linked_po_numbers:
            po_notification_filter |= Q(
                category="PO",
                title__icontains=po_number,
            )
            po_notification_filter |= Q(
                category="PO",
                message__icontains=po_number,
            )

        if linked_po_ids:
            Notification.objects.filter(
                po_notification_filter
            ).delete()

        # ------------------------------------------------------
        # 6. Delete PO items and the linked POs.
        # Explicit item deletion works even when an older schema
        # does not use CASCADE on PurchaseOrderItem.
        # ------------------------------------------------------
        deleted_po_item_rows = 0
        deleted_po_rows = 0

        if linked_po_ids:
            deleted_po_item_rows, _ = (
                PurchaseOrderItem.objects
                .filter(
                    purchase_order_id__in=(
                        linked_po_ids
                    )
                )
                .delete()
            )

            deleted_po_rows, _ = (
                PurchaseOrder.objects
                .filter(
                    id__in=linked_po_ids
                )
                .delete()
            )

        # ------------------------------------------------------
        # 7. Delete all notifications belonging to this MR.
        # reference_id normally stores the MR database ID.
        # Older records may store the public MR number instead.
        # ------------------------------------------------------
        mr_reference_values = [
            str(material_request.id),
            mr_number,
        ]

        Notification.objects.filter(
            Q(
                category="MR",
                reference_id__in=(
                    mr_reference_values
                ),
            )
            | Q(
                category="MR",
                title__icontains=mr_number,
            )
            | Q(
                category="MR",
                message__icontains=mr_number,
            )
        ).delete()

        # ------------------------------------------------------
        # 8. These two models use on_delete=PROTECT, so they must
        # be removed before deleting MaterialRequest.
        # Removing a reservation releases undeducted physical stock
        # because the actual Inventory quantity was not changed.
        # ------------------------------------------------------
        deleted_project_rows, _ = (
            ProjectInventory.objects
            .filter(
                material_request=material_request
            )
            .delete()
        )

        deleted_reservation_rows, _ = (
            InventoryReservation.objects
            .filter(
                material_request=material_request
            )
            .delete()
        )

        # ------------------------------------------------------
        # 9. Delete the MaterialRequest last.
        # BOMItem and RDItem rows are removed through CASCADE.
        # ------------------------------------------------------
        material_request.delete()

        return Response(
            {
                "detail": (
                    "Material Request and complete linked "
                    "workflow deleted successfully."
                ),
                "deleted_id": deleted_id,
                "material_request_id": mr_number,
                "deleted": {
                    "purchase_orders": (
                        len(linked_po_ids)
                    ),
                    "purchase_order_items": (
                        deleted_po_item_rows
                    ),
                    "inward_entries": (
                        len(linked_inward_ids)
                    ),
                    "inward_delete_rows": (
                        deleted_inward_rows
                    ),
                    "project_inventory_rows": (
                        deleted_project_rows
                    ),
                    "inventory_reservations": (
                        deleted_reservation_rows
                    ),
                    "old_accidental_inventory_rows": (
                        deleted_inventory_rows
                    ),
                },
            },
            status=status.HTTP_200_OK,
        )