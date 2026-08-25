from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.contrib.staticfiles import finders
from inventory.models import InventoryReservation
from materialrequest.models import MaterialRequest
from notifications.models import Notification
from notifications.email_service import send_ipms_email
from users.models import User
from django.conf import settings
from decimal import Decimal
from io import BytesIO

from django.http import HttpResponse
from django.template.loader import get_template

from num2words import num2words
from xhtml2pdf import pisa

from vendors.models import Vendor
from .models import (
    PurchaseOrder,
    PurchaseOrderApproval,
    PurchaseOrderItem,
    PurchaseRequest,
)
from .serializers import (
    PurchaseOrderSerializer,
    PurchaseRequestSerializer,
)
from pathlib import Path


def pdf_link_callback(uri, rel):
    if uri == "font://pdfunicode":

        font_path = Path(r"C:\Windows\Fonts\NotoSans-Regular.ttf")
        if font_path.exists():
            return font_path.as_uri()

        possible_fonts = [
            Path(r"C:\Windows\Fonts\arialuni.ttf"),
            Path(r"C:\Windows\Fonts\arial.ttf"),
            Path(r"C:\Windows\Fonts\seguisym.ttf"),
            Path(r"C:\Windows\Fonts\segoeui.ttf"),
        ]

        for font_path in possible_fonts:
            if font_path.exists():
                return font_path.as_uri()

        raise FileNotFoundError(
            "No suitable Unicode font was found in C:\\Windows\\Fonts"
        )

    return uri
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

    # Parse JWT when the caller sends one, but preserve the
    # existing AllowAny behavior for routes that still rely on it.
    authentication_classes = [JWTAuthentication]
    permission_classes = [AllowAny]

    # ==========================================================
    # QC REPLACEMENT APPROVAL HELPERS
    # ==========================================================

    @staticmethod
    def normalize_role(value):
        return str(value or "").strip().lower()

    def get_user_roles(self, user):
        """
        Return every role currently assigned to the user.

        The primary role remains in `role`. Extra allowed roles are read
        from `additional_roles`, or from get_all_roles() when provided by
        the User model.
        """
        if not user or not getattr(user, "is_authenticated", False):
            return []

        if hasattr(user, "get_all_roles"):
            try:
                roles = user.get_all_roles()
            except Exception:
                roles = []
        else:
            roles = []

            primary_role = self.normalize_role(
                getattr(user, "role", "")
            )
            if primary_role:
                roles.append(primary_role)

            additional_roles = getattr(
                user,
                "additional_roles",
                [],
            )

            if not isinstance(additional_roles, list):
                additional_roles = []

            for value in additional_roles:
                normalized = self.normalize_role(value)
                if normalized and normalized not in roles:
                    roles.append(normalized)

        normalized_roles = []

        for value in roles or []:
            normalized = self.normalize_role(value)
            if normalized and normalized not in normalized_roles:
                normalized_roles.append(normalized)

        return normalized_roles

    def get_request_active_role(self, request):
        """
        Return the active JWT role only when it is still assigned in DB.

        A stale token must not keep removed access. If Admin changed the
        user's primary role and the old JWT role is no longer assigned,
        fall back to the current primary role.
        """
        user = getattr(request, "user", None)

        if not user or not getattr(user, "is_authenticated", False):
            return ""

        allowed_roles = self.get_user_roles(user)

        token = getattr(request, "auth", None)
        token_role = ""

        if token is not None:
            try:
                token_role = self.normalize_role(
                    token.get("active_role", "")
                )
            except (AttributeError, TypeError, ValueError):
                token_role = ""

        if token_role and token_role in allowed_roles:
            return token_role

        primary_role = self.normalize_role(
            getattr(user, "role", "")
        )

        if primary_role and primary_role in allowed_roles:
            return primary_role

        return allowed_roles[0] if allowed_roles else ""

    def require_active_role(self, request, *allowed_roles):
        role = self.get_request_active_role(request)

        allowed = {
            self.normalize_role(value)
            for value in allowed_roles
            if value
        }

        if not role:
            raise PermissionDenied("Authentication is required.")

        if role not in allowed:
            raise PermissionDenied(
                "This action is not available for the current active role "
                f"'{role}'. Switch to one of: "
                + ", ".join(sorted(allowed))
                + "."
            )

        return role

    def get_request_actor_name(self, request):
        user = getattr(request, "user", None)

        if not user or not getattr(user, "is_authenticated", False):
            return "User"

        return str(
            getattr(user, "email", "")
            or getattr(user, "employee_name", "")
            or getattr(user, "name", "")
            or getattr(user, "username", "")
            or "User"
        ).strip()[:100]

    def save_replacement_notification(
        self,
        purchase_order,
        *,
        receiver,
        notification_status,
        title,
        message,
        requested_by="",
    ):
        """Keep one current replacement notification per PO + receiver."""
        queryset = Notification.objects.filter(
            category="PO",
            receiver=str(receiver).upper(),
            reference_id=str(purchase_order.id),
        ).order_by("-created_at", "-id")

        notification = queryset.first()
        requested_by = str(requested_by or "").strip()[:150]

        if notification:
            notification.title = title
            notification.message = message
            notification.status = notification_status
            notification.is_read = False
            if requested_by:
                notification.requested_by = requested_by

            update_fields = [
                "title",
                "message",
                "status",
                "is_read",
            ]
            if requested_by:
                update_fields.append("requested_by")

            notification.save(update_fields=update_fields)
            queryset.exclude(pk=notification.pk).delete()
            return notification

        create_kwargs = {
            "category": "PO",
            "receiver": str(receiver).upper(),
            "reference_id": str(purchase_order.id),
            "title": title,
            "message": message,
            "status": notification_status,
            "is_read": False,
        }
        if requested_by:
            create_kwargs["requested_by"] = requested_by

        return Notification.objects.create(**create_kwargs)

    @staticmethod
    def get_replacement_mr_status(replacement_orders):
        """
        Return the most important current replacement state for an MR.

        When several components have replacement orders at the same time,
        an unresolved approval/delivery stage takes precedence over a
        replacement that has already been received.
        """
        statuses = {
            str(order.status or "").strip().upper()
            for order in replacement_orders
        }

        if not statuses:
            return ""

        if statuses & {
            "REPLACEMENT_MANAGER_REJECTED",
            "REPLACEMENT_FINANCE_REJECTED",
        }:
            return "REPLACEMENT_APPROVAL_REJECTED"

        if statuses & {
            "REPLACEMENT_PENDING_MANAGER",
            "REPLACEMENT_PENDING_FINANCE",
        }:
            return "AWAITING_REPLACEMENT_APPROVAL"

        if "REPLACEMENT_APPROVED" in statuses:
            return "REPLACEMENT_APPROVED"

        if "REPLACEMENT_ORDERED" in statuses:
            return "AWAITING_REPLACEMENT_DELIVERY"

        if "REPLACEMENT_PARTIALLY_RECEIVED" in statuses:
            return "REPLACEMENT_PARTIALLY_RECEIVED"

        if statuses == {"REPLACEMENT_RECEIVED"}:
            return "REPLACEMENT_RECEIVED"

        return ""

    def sync_replacement_mr_status(self, purchase_order):
        source_mr_number = str(
            purchase_order.source_mr_number or ""
        ).strip()

        if not source_mr_number:
            return None

        material_request = self.get_source_material_request(
            source_mr_number,
            lock=True,
        )
        if not material_request:
            return None

        current_status = str(material_request.status or "").upper()
        if current_status in {"INVENTORY_ISSUED", "MR_COMPLETED"}:
            return material_request

        replacement_orders = list(
            PurchaseOrder.objects
            .select_for_update()
            .filter(
                source_mr_number=str(
                    material_request.material_request_id
                ).strip(),
                order_type="REPLACEMENT",
            )
            .exclude(
                status__in=[
                    "REJECTED",
                    "FINANCE_REJECTED",
                ]
            )
        )

        desired_status = self.get_replacement_mr_status(
            replacement_orders
        )

        if desired_status and desired_status != current_status:
            material_request.status = desired_status
            material_request.po_raised = True
            material_request.save(
                update_fields=["status", "po_raised"]
            )

        return material_request

    def get_replacement_po_or_error(self, pk):
        try:
            purchase_order = (
                PurchaseOrder.objects
                .select_for_update()
                .prefetch_related("items", "items__component")
                .get(pk=pk)
            )
        except PurchaseOrder.DoesNotExist:
            return None, Response(
                {"detail": "Purchase Order not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if str(purchase_order.order_type or "").upper() != "REPLACEMENT":
            return None, Response(
                {"detail": "This action is only for QC replacement orders."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return purchase_order, None


    # ==========================================================
    # ORIGINAL PO SENDER / FINANCE NOTIFICATION HELPERS
    # ==========================================================

    def get_authenticated_po_sender_name(self):
        """
        Return a stable identity for the user who creates/raises the PO.

        IMPORTANT:
        Prefer the authenticated user's email because employee display
        names can differ (for example "Karthik" vs "Karthik S").
        Notification.requested_by can store up to 150 characters, so the
        company email is safe and gives us an exact user match later.
        """
        user = getattr(
            self.request,
            "user",
            None,
        )

        if (
            not user
            or not getattr(
                user,
                "is_authenticated",
                False,
            )
        ):
            return ""

        email = str(
            getattr(
                user,
                "email",
                "",
            )
            or ""
        ).strip()

        if email:
            return email[:150]

        return str(
            self.get_user_display_name(
                user,
                "",
            )
            or ""
        ).strip()[:150]

    def save_finance_notification_with_sender(
        self,
        purchase_order,
    ):
        """
        Create/update the Finance notification WITHOUT losing the
        Procurement user who raised/sent the PO.

        The previous implementation deleted the old Finance
        notification and recreated it without requested_by. That
        erased the sender identity, so Finance's Approved/Rejected
        email had nobody to return to.
        """
        queryset = (
            Notification.objects
            .filter(
                category="PO",
                receiver="FINANCE",
                reference_id=str(
                    purchase_order.id
                ),
            )
            .order_by(
                "-created_at",
                "-id",
            )
        )

        # Keep any sender already stored by an earlier notification.
        preserved_sender = (
            queryset
            .exclude(
                requested_by__isnull=True
            )
            .exclude(
                requested_by=""
            )
            .values_list(
                "requested_by",
                flat=True,
            )
            .first()
            or ""
        )

        # Prefer the currently authenticated PO-raising user.
        actor_name = (
            self.get_authenticated_po_sender_name()
        )

        requested_by = (
            actor_name
            or str(
                preserved_sender or ""
            ).strip()
        )

        notification = queryset.first()

        if notification:
            notification.title = (
                "PO Approval Request - "
                f"{purchase_order.po_number}"
            )

            notification.message = (
                "Approval requested for PO "
                f"{purchase_order.po_number}"
            )

            notification.status = (
                "PENDING_FINANCE"
            )
            notification.receiver = "FINANCE"
            notification.is_read = False

            # Never overwrite a valid original sender with blank.
            if requested_by:
                notification.requested_by = (
                    requested_by
                )

            notification.save(
                update_fields=[
                    "title",
                    "message",
                    "status",
                    "receiver",
                    "is_read",
                    "requested_by",
                ]
                if requested_by
                else [
                    "title",
                    "message",
                    "status",
                    "receiver",
                    "is_read",
                ]
            )

            # Keep only one Finance notification per PO.
            queryset.exclude(
                pk=notification.pk
            ).delete()

            return notification

        create_kwargs = {
            "category": "PO",
            "title": (
                "PO Approval Request - "
                f"{purchase_order.po_number}"
            ),
            "message": (
                "Approval requested for PO "
                f"{purchase_order.po_number}"
            ),
            "reference_id": str(
                purchase_order.id
            ),
            "status": "PENDING_FINANCE",
            "receiver": "FINANCE",
            "is_read": False,
        }

        if requested_by:
            create_kwargs[
                "requested_by"
            ] = requested_by

        return Notification.objects.create(
            **create_kwargs
        )


    # ==========================================================
    # DIRECT PO - MANAGER APPROVAL
    # Finance -> Manager -> Procurement can order
    # ==========================================================

    def is_direct_standard_po(self, purchase_order):
        return (
            str(
                getattr(
                    purchase_order,
                    "order_type",
                    "STANDARD",
                )
                or "STANDARD"
            ).strip().upper()
            != "REPLACEMENT"
            and not str(
                getattr(
                    purchase_order,
                    "source_mr_number",
                    "",
                )
                or ""
            ).strip()
        )

    def save_direct_po_manager_notification(
        self,
        purchase_order,
    ):
        """
        After Finance approves a Direct PO, create exactly one
        Manager notification asking for final Manager approval.
        """
        finance_notification = (
            Notification.objects
            .filter(
                category="PO",
                receiver="FINANCE",
                reference_id=str(
                    purchase_order.id
                ),
            )
            .order_by("-created_at", "-id")
            .first()
        )

        requested_by = str(
            getattr(
                finance_notification,
                "requested_by",
                "",
            )
            or self.get_authenticated_po_sender_name()
            or ""
        ).strip()[:150]

        queryset = (
            Notification.objects
            .filter(
                category="PO",
                receiver="MANAGER",
                reference_id=str(
                    purchase_order.id
                ),
            )
            .order_by("-created_at", "-id")
        )

        notification = queryset.first()

        title = (
            "Direct PO Manager Approval - "
            f"{purchase_order.po_number}"
        )

        message = (
            f"Finance approved Direct PO "
            f"{purchase_order.po_number}. "
            "Manager approval is required before "
            "Procurement can mark this PO as Ordered."
        )

        if notification:
            notification.title = title
            notification.message = message
            notification.status = "PENDING_MANAGER"
            notification.receiver = "MANAGER"
            notification.is_read = False

            if requested_by:
                notification.requested_by = (
                    requested_by
                )

            fields = [
                "title",
                "message",
                "status",
                "receiver",
                "is_read",
            ]

            if requested_by:
                fields.append("requested_by")

            notification.save(
                update_fields=fields
            )

            queryset.exclude(
                pk=notification.pk
            ).delete()

            return notification

        create_kwargs = {
            "category": "PO",
            "receiver": "MANAGER",
            "reference_id": str(
                purchase_order.id
            ),
            "title": title,
            "message": message,
            "status": "PENDING_MANAGER",
            "is_read": False,
        }

        if requested_by:
            create_kwargs[
                "requested_by"
            ] = requested_by

        return Notification.objects.create(
            **create_kwargs
        )

    def send_direct_po_manager_approval_email(
        self,
        purchase_order_id,
    ):
        """
        Email all active Manager users after Finance approves
        a Direct PO. This email is only for Direct STANDARD POs.
        """
        try:
            purchase_order = (
                PurchaseOrder.objects
                .prefetch_related(
                    "items",
                    "items__component",
                )
                .get(pk=purchase_order_id)
            )
        except PurchaseOrder.DoesNotExist:
            return False

        if not self.is_direct_standard_po(
            purchase_order
        ):
            return False

        manager_users = [
            user
            for user in (
                User.objects
                .filter(is_active=True)
                .exclude(email__isnull=True)
                .exclude(email="")
                .order_by("id")
            )
            if "manager" in self.get_user_roles(
                user
            )
        ]

        if not manager_users:
            print(
                "DIRECT PO MANAGER EMAIL SKIPPED:",
                purchase_order.po_number,
                "- no active Manager user with email.",
            )
            return False

        items = list(
            purchase_order.items.all()
        )

        total_quantity = sum(
            max(
                int(item.quantity or 0),
                0,
            )
            for item in items
        )

        component_lines = []

        for item in items:
            component = getattr(
                item,
                "component",
                None,
            )

            component_code = str(
                getattr(
                    component,
                    "component_id",
                    "",
                )
                or getattr(
                    component,
                    "id",
                    "",
                )
                or ""
            ).strip()

            component_name = str(
                getattr(
                    component,
                    "name",
                    "",
                )
                or "Component"
            ).strip()

            component_lines.append(
                (
                    f"{component_code} - "
                    f"{component_name} "
                    f"(Qty: {int(item.quantity or 0)})"
                ).strip(" -")
            )

        components_display = (
            "; ".join(component_lines)
            if component_lines
            else "-"
        )

        subject = (
            f"{purchase_order.po_number} "
            "- Manager Approval Required"
        )

        message = (
            f"Finance has approved Direct Purchase Order "
            f"{purchase_order.po_number}. "
            "Manager approval is required before Procurement "
            "can place the order."
        )

        action_url = (
            f"{self.get_ipms_base_url()}"
            "/purchase-orders"
        )

        sent_any = False

        for manager_user in manager_users:
            sent = send_ipms_email(
                recipient_email=
                    manager_user.email,
                subject=subject,
                context={
                    "recipient_name":
                        self.get_user_display_name(
                            manager_user,
                            "Manager",
                        ),
                    "message": message,
                    "table_headers": [
                        "PO Number",
                        "PO Type",
                        "Vendor",
                        "Quantity",
                        "Components",
                        "Finance Status",
                        "Manager Status",
                    ],
                    "table_values": [
                        purchase_order.po_number,
                        "Direct PO",
                        purchase_order.vendor_name
                        or "-",
                        total_quantity,
                        components_display,
                        "Finance Approved",
                        "Pending Manager Approval",
                    ],
                    "status":
                        "Pending Manager Approval",
                    "instruction": (
                        "Please open the Purchase Order "
                        "table in IPMS and approve this "
                        "Direct PO. Procurement can mark "
                        "it Ordered only after your approval."
                    ),
                    "button_text":
                        "Review Direct PO",
                    "action_url":
                        action_url,
                },
            )

            if sent:
                sent_any = True

        return sent_any

    # ==========================================================
    # FINANCE APPROVAL EMAIL
    # ==========================================================

    @staticmethod
    def get_user_display_name(user, fallback="User"):
        if not user:
            return fallback

        return (
            getattr(user, "employee_name", "")
            or getattr(user, "name", "")
            or getattr(user, "username", "")
            or getattr(user, "email", "")
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
        ).rstrip("/")

    def send_finance_approval_email(
        self,
        purchase_order_id,
    ):
        """
        Send one Finance approval-request email to every active
        Finance user.

        Works for:
        - Direct Purchase Orders.
        - Purchase Orders raised from an approved Material Request.
        """
        try:
            purchase_order = (
                PurchaseOrder.objects
                .prefetch_related(
                    "items",
                    "items__component",
                )
                .get(pk=purchase_order_id)
            )
        except PurchaseOrder.DoesNotExist:
            return False

        finance_users = (
            User.objects
            .filter(
                role__iexact="finance",
                is_active=True,
            )
            .exclude(email__isnull=True)
            .exclude(email="")
            .order_by("id")
        )

        if not finance_users.exists():
            print(
                "FINANCE EMAIL SKIPPED: "
                "No active Finance user with an email address."
            )
            return False

        items = list(
            purchase_order.items.all()
        )

        total_quantity = 0
        subtotal = Decimal("0.00")
        gst_total = Decimal("0.00")
        component_lines = []

        for item in items:
            quantity = max(
                int(item.quantity or 0),
                0,
            )

            unit_price = Decimal(
                str(
                    item.unit_price
                    or Decimal("0.00")
                )
            )

            gst_percentage = Decimal(
                str(
                    item.gst_percentage
                    or Decimal("0.00")
                )
            )

            line_subtotal = (
                Decimal(quantity)
                * unit_price
            )

            line_gst = (
                line_subtotal
                * gst_percentage
                / Decimal("100")
            )

            total_quantity += quantity
            subtotal += line_subtotal
            gst_total += line_gst

            component = getattr(
                item,
                "component",
                None,
            )

            component_code = (
                getattr(
                    component,
                    "component_id",
                    "",
                )
                or getattr(
                    component,
                    "id",
                    "",
                )
                or ""
            )

            component_name = (
                getattr(
                    component,
                    "name",
                    "",
                )
                or "Component"
            )

            component_lines.append(
                (
                    f"{component_code} - "
                    f"{component_name} "
                    f"(Qty: {quantity})"
                ).strip()
            )

        grand_total = (
            subtotal + gst_total
        )

        source_mr = str(
            purchase_order.source_mr_number
            or ""
        ).strip()

        mr_display = (
            source_mr
            if source_mr
            else "Direct PO"
        )

        po_date = (
            purchase_order.ordered_date
            or (
                purchase_order.created_at.date()
                if purchase_order.created_at
                else None
            )
        )

        expected_delivery = (
            purchase_order.expected_delivery_date
            or "-"
        )

        components_display = (
            "; ".join(component_lines)
            if component_lines
            else "-"
        )

        subject = (
            f"{purchase_order.po_number} "
            "- Finance Approval Required"
        )

        action_url = (
            f"{self.get_ipms_base_url()}"
            "/finance/notifications"
        )

        sent_any = False

        for finance_user in finance_users:
            sent = send_ipms_email(
                recipient_email=finance_user.email,
                subject=subject,
                context={
                    "recipient_name":
                        self.get_user_display_name(
                            finance_user,
                            "Finance",
                        ),

                    "message": (
                        "A Purchase Order has been "
                        "raised in IPMS and is "
                        "awaiting Finance approval."
                    ),

                    "table_headers": [
                        "PO Number",
                        "MR ID",
                        "Vendor",
                        "Quantity",
                        "Order Total",
                        "PO Date",
                        "Expected Delivery",
                        "Status",
                    ],

                    "table_values": [
                        purchase_order.po_number,
                        mr_display,
                        purchase_order.vendor_name
                        or "-",
                        total_quantity,
                        f"INR {grand_total:.2f}",
                        str(po_date or "-"),
                        str(expected_delivery),
                        "Pending Finance",
                    ],

                    "status":
                        "Pending Finance",

                    "instruction": (
                        "Please review the Purchase "
                        "Order in IPMS and approve "
                        "or reject it."
                    ),

                    "button_text":
                        "Review PO in IPMS",

                    "action_url":
                        action_url,

                    "components":
                        components_display,
                },
            )

            if sent:
                sent_any = True

        return sent_any



    # ==========================================================
    # MR REQUESTER - PO RAISED EMAIL
    # ==========================================================

    def send_mr_requester_po_raised_email(
        self,
        purchase_order_id,
    ):
        """
        Inform the original MR requester whenever Procurement raises a
        Purchase Order linked to that Material Request.

        Direct Purchase Orders are intentionally excluded because they
        have no Material Request requester.
        """
        try:
            purchase_order = (
                PurchaseOrder.objects
                .prefetch_related(
                    "items",
                    "items__component",
                )
                .get(pk=purchase_order_id)
            )
        except PurchaseOrder.DoesNotExist:
            return False

        source_mr_number = str(
            purchase_order.source_mr_number
            or ""
        ).strip()

        if not source_mr_number:
            return False

        material_request = (
            self.get_source_material_request(
                source_mr_number
            )
        )

        if not material_request:
            return False

        requester = getattr(
            material_request,
            "requester",
            None,
        )

        requester_email = str(
            getattr(
                requester,
                "email",
                "",
            )
            or ""
        ).strip()

        if not requester_email:
            # Do not guess an address from requester_name.
            return False

        requester_name = (
            material_request.requester_name
            or self.get_user_display_name(
                requester,
                "Requester",
            )
        )

        items = list(
            purchase_order.items.all()
        )

        total_quantity = sum(
            max(
                int(item.quantity or 0),
                0,
            )
            for item in items
        )

        component_lines = []

        for item in items:
            component = getattr(
                item,
                "component",
                None,
            )

            component_code = (
                getattr(
                    component,
                    "component_id",
                    "",
                )
                or getattr(
                    component,
                    "id",
                    "",
                )
                or ""
            )

            component_name = (
                getattr(
                    component,
                    "name",
                    "",
                )
                or "Component"
            )

            component_lines.append(
                (
                    f"{component_code} - "
                    f"{component_name} "
                    f"(Qty: {int(item.quantity or 0)})"
                ).strip()
            )

        component_summary = (
            "; ".join(component_lines)
            if component_lines
            else "-"
        )

        raised_by = (
            self.get_authenticated_po_sender_name()
            or "Procurement"
        )

        subject = (
            f"{purchase_order.po_number} raised for "
            f"{material_request.material_request_id} "
            f"- Procurement Update"
        )

        approval_state = str(
            purchase_order.approval_status
            or purchase_order.status
            or "PO_RAISED"
        ).strip().upper()

        if approval_state == "PENDING_FINANCE":
            status_label = (
                "PO Raised - Pending Finance"
            )
        else:
            status_label = "PO Raised"

        return send_ipms_email(
            recipient_email=requester_email,
            subject=subject,
            context={
                "recipient_name": requester_name,
                "message": (
                    f"Procurement has raised Purchase Order "
                    f"{purchase_order.po_number} for your "
                    f"Material Request "
                    f"{material_request.material_request_id}."
                ),
                "table_headers": [
                    "MR ID",
                    "PO Number",
                    "Project",
                    "Vendor",
                    "PO Quantity",
                    "Components",
                    "Raised By",
                    "Status",
                ],
                "table_values": [
                    material_request.material_request_id,
                    purchase_order.po_number,
                    material_request.project,
                    purchase_order.vendor_name
                    or "-",
                    total_quantity,
                    component_summary,
                    raised_by,
                    status_label,
                ],
                "status": status_label,
                "instruction": (
                    "This is an informational update. "
                    "You can continue tracking the Material "
                    "Request and its Purchase Order in IPMS."
                ),
                "button_text": (
                    "View Material Request in IPMS"
                ),
                "action_url": (
                    f"{self.get_ipms_base_url()}"
                    f"/material-requests"
                ),
            },
        )


    # ==========================================================
    # PO REQUESTER RESULT EMAIL
    # ==========================================================

    def resolve_po_requester_user(
        self,
        purchase_order,
    ):
        """
        Resolve the exact user who created/raised the PO.

        Preferred source:
        Notification.requested_by for the Finance notification.

        New PO records store the creator's company email there.
        Older records may contain only an employee name or an email
        local-part, so several safe matching fallbacks are supported.
        """

        notification = (
            Notification.objects
            .filter(
                category="PO",
                receiver="FINANCE",
                reference_id=str(
                    purchase_order.id
                ),
            )
            .exclude(
                requested_by__isnull=True
            )
            .exclude(
                requested_by=""
            )
            .order_by(
                "-created_at",
                "-id",
            )
            .first()
        )

        requested_by = ""

        if notification:
            requested_by = str(
                getattr(
                    notification,
                    "requested_by",
                    "",
                )
                or ""
            ).strip()

        # Older records may have the sender in PurchaseOrderApproval.
        if not requested_by:
            try:
                latest_approval = (
                    purchase_order
                    .approvals
                    .exclude(
                        requested_by__isnull=True
                    )
                    .exclude(
                        requested_by=""
                    )
                    .order_by(
                        "-created_at",
                        "-id",
                    )
                    .first()
                )
            except Exception:
                latest_approval = None

            if latest_approval:
                requested_by = str(
                    getattr(
                        latest_approval,
                        "requested_by",
                        "",
                    )
                    or ""
                ).strip()

        if not requested_by:
            print(
                "PO RESULT EMAIL: no original sender stored for",
                purchase_order.po_number,
            )
            return None

        # ---------------------------------------------------------
        # 1. Exact company email match.
        # ---------------------------------------------------------
        if "@" in requested_by:
            user = (
                User.objects
                .filter(
                    email__iexact=requested_by,
                    is_active=True,
                )
                .first()
            )

            if user:
                return user

        # ---------------------------------------------------------
        # 2. Exact employee-name match.
        # ---------------------------------------------------------
        user = (
            User.objects
            .filter(
                employee_name__iexact=requested_by,
                is_active=True,
            )
            .first()
        )

        if user:
            return user

        # ---------------------------------------------------------
        # 3. Exact username match, if this custom User has username.
        # ---------------------------------------------------------
        try:
            user = (
                User.objects
                .filter(
                    username__iexact=requested_by,
                    is_active=True,
                )
                .first()
            )
        except Exception:
            user = None

        if user:
            return user

        # ---------------------------------------------------------
        # 4. Email local-part match.
        #    Example: requested_by="karthik.s"
        #             email="karthik.s@aero360.co.in"
        # ---------------------------------------------------------
        requested_lower = requested_by.lower()

        for candidate in (
            User.objects
            .filter(is_active=True)
            .exclude(email__isnull=True)
            .exclude(email="")
        ):
            email = str(
                getattr(
                    candidate,
                    "email",
                    "",
                )
                or ""
            ).strip()

            if (
                email
                and email.split("@")[0].lower()
                == requested_lower
            ):
                return candidate

        # ---------------------------------------------------------
        # 5. Normalized employee-name fallback.
        #    This handles harmless spacing/punctuation differences,
        #    but does not choose a user unless the match is unique.
        # ---------------------------------------------------------
        def normalize_identity(value):
            return "".join(
                ch
                for ch in str(
                    value or ""
                ).lower()
                if ch.isalnum()
            )

        requested_normalized = normalize_identity(
            requested_by
        )

        matches = []

        if requested_normalized:
            for candidate in (
                User.objects
                .filter(is_active=True)
            ):
                employee_name = normalize_identity(
                    getattr(
                        candidate,
                        "employee_name",
                        "",
                    )
                )

                if (
                    employee_name
                    and employee_name
                    == requested_normalized
                ):
                    matches.append(
                        candidate
                    )

        if len(matches) == 1:
            return matches[0]

        print(
            "PO RESULT EMAIL: unable to resolve original sender",
            requested_by,
            "for",
            purchase_order.po_number,
        )

        return None

    def send_po_requester_result_email(
        self,
        purchase_order_id,
        *,
        outcome,
    ):
        """
        After Finance approves or rejects a PO, send the result
        back to the user who originally sent that PO for Finance
        approval.
        """
        try:
            purchase_order = (
                PurchaseOrder.objects
                .prefetch_related(
                    "items",
                    "items__component",
                )
                .get(
                    pk=purchase_order_id
                )
            )
        except PurchaseOrder.DoesNotExist:
            return False

        requester = (
            self.resolve_po_requester_user(
                purchase_order
            )
        )

        if (
            not requester
            or not str(
                getattr(
                    requester,
                    "email",
                    "",
                )
                or ""
            ).strip()
        ):
            print(
                "PO RESULT EMAIL SKIPPED:",
                purchase_order.po_number,
                "- original PO sender could "
                "not be resolved from the "
                "Finance notification.",
            )
            return False

        normalized_outcome = str(
            outcome or ""
        ).strip().lower()

        is_approved = (
            normalized_outcome
            == "approved"
        )

        result_label = (
            "Finance Approved"
            if is_approved
            else "Finance Rejected"
        )

        items = list(
            purchase_order.items.all()
        )

        total_quantity = 0
        subtotal = Decimal("0.00")
        gst_total = Decimal("0.00")

        for item in items:
            quantity = max(
                int(item.quantity or 0),
                0,
            )

            unit_price = Decimal(
                str(
                    item.unit_price
                    or Decimal("0.00")
                )
            )

            gst_percentage = Decimal(
                str(
                    item.gst_percentage
                    or Decimal("0.00")
                )
            )

            line_subtotal = (
                Decimal(quantity)
                * unit_price
            )

            line_gst = (
                line_subtotal
                * gst_percentage
                / Decimal("100")
            )

            total_quantity += quantity
            subtotal += line_subtotal
            gst_total += line_gst

        grand_total = (
            subtotal + gst_total
        )

        source_mr = str(
            purchase_order.source_mr_number
            or ""
        ).strip()

        mr_display = (
            source_mr
            if source_mr
            else "Direct PO"
        )

        finance_remarks = str(
            getattr(
                purchase_order,
                "finance_remarks",
                "",
            )
            or ""
        ).strip()

        rejection_reason = str(
            getattr(
                purchase_order,
                "rejection_reason",
                "",
            )
            or ""
        ).strip()

        decision_reason = (
            finance_remarks
            or rejection_reason
            or "-"
        )

        decision_by = (
            getattr(
                purchase_order,
                "approved_by",
                "",
            )
            if is_approved
            else getattr(
                purchase_order,
                "rejected_by",
                "",
            )
        )

        decision_by = str(
            decision_by or "Finance"
        ).strip()

        if is_approved:
            subject = (
                f"{purchase_order.po_number} "
                "- Finance Approved"
            )

            message = (
                "Your Purchase Order has been "
                "approved by Finance."
            )

            instruction = (
                "You can now continue the "
                "approved Purchase Order workflow "
                "in IPMS."
            )
        else:
            subject = (
                f"{purchase_order.po_number} "
                "- Finance Rejected"
            )

            message = (
                "Your Purchase Order has been "
                "rejected by Finance."
            )

            instruction = (
                "Please review the Finance "
                "remarks/rejection reason in IPMS "
                "before taking further action."
            )

        action_url = (
            f"{self.get_ipms_base_url()}"
            "/purchase-orders"
        )

        print(
            "PO RESULT EMAIL:",
            purchase_order.po_number,
            "->",
            requester.email,
            "(" + result_label + ")",
        )

        sent = send_ipms_email(
            recipient_email=
                requester.email,
            subject=subject,
            context={
                "recipient_name":
                    self.get_user_display_name(
                        requester,
                        "Procurement",
                    ),

                "message":
                    message,

                "table_headers": [
                    "PO Number",
                    "MR ID",
                    "Vendor",
                    "Quantity",
                    "Order Total",
                    "Finance Decision",
                    "Decision By",
                    (
                        "Finance Remarks"
                        if is_approved
                        else "Rejection Reason"
                    ),
                ],

                "table_values": [
                    purchase_order.po_number,
                    mr_display,
                    purchase_order.vendor_name
                    or "-",
                    total_quantity,
                    f"INR {grand_total:.2f}",
                    result_label,
                    decision_by,
                    decision_reason,
                ],

                "status":
                    result_label,

                "instruction":
                    instruction,

                "button_text":
                    "Open Purchase Order in IPMS",

                "action_url":
                    action_url,
            },
        )

        print(
            "PO RESULT EMAIL SENT =",
            sent,
            "for",
            purchase_order.po_number,
        )

        return sent


    # ==========================================================
    # PURCHASE ORDER PDF
    # ==========================================================

    @action(
        detail=True,
        methods=["get"],
        url_path="pdf",
    )
    def download_pdf(self, request, pk=None):
        try:
            purchase_order = (
                PurchaseOrder.objects
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
                status=status.HTTP_404_NOT_FOUND,
            )

        # ------------------------------------------------------
        # Find Vendor
        # ------------------------------------------------------

        vendor = (
            Vendor.objects
            .filter(
                name__iexact=purchase_order.vendor_name
            )
            .first()
        )

        # ------------------------------------------------------
        # Purchase Order Date
        # ------------------------------------------------------

        po_date = (
            purchase_order.ordered_date
            or (
                purchase_order.created_at.date()
                if purchase_order.created_at
                else None
            )
        )

        # ------------------------------------------------------
        # Items / totals
        # ------------------------------------------------------

        pdf_items = []

        subtotal = Decimal("0.00")
        gst_total = Decimal("0.00")
        total_quantity = 0

        for index, item in enumerate(
            purchase_order.items.all(),
            start=1,
        ):
            component = item.component

            quantity = int(
                item.quantity or 0
            )

            unit_price = (
                item.unit_price
                or Decimal("0.00")
            )

            line_subtotal = (
                Decimal(quantity) *
                unit_price
            )

            gst_percentage = (
                item.gst_percentage
                or Decimal("0.00")
            )

            gst_amount = (
                line_subtotal *
                gst_percentage /
                Decimal("100")
            )

            subtotal += line_subtotal
            gst_total += gst_amount
            total_quantity += quantity

            pdf_items.append({
                "sl_no": index,

                "name":
                    component.name
                    if component
                    else "Component",

                "component_id":
                    component.component_id
                    if component
                    else "",

                "part_number":
                    component.part_numbers
                    if component
                    else "",

                "specification":
                    component.specifications
                    if component
                    else "",

                "hsn":
                    component.hsn_numbers
                    if component
                    else "",

"uom": "Nos",

                "due_date":
                    purchase_order.expected_delivery_date,

                "quantity":
                    quantity,

                "unit_price":
                    unit_price,

                "gst_percentage":
                    gst_percentage,

                "subtotal":
                    line_subtotal,
            })

        grand_total = (
            subtotal +
            gst_total
        )

        # ------------------------------------------------------
        # GST split
        #
        # Dronix is Tamil Nadu - State code 33.
        #
        # Tamil Nadu vendor:
        # CGST + SGST
        #
        # Outside Tamil Nadu:
        # IGST
        # ------------------------------------------------------

        # ------------------------------------------------------
        # GST FROM PURCHASE ORDER ONLY
        # ------------------------------------------------------

        gst_rates = []

        for po_item in purchase_order.items.all():
            rate = Decimal(
                str(po_item.gst_percentage or 0)
            )

            if rate not in gst_rates:
                gst_rates.append(rate)

        # Do not display GST percentage in PDF.
        gst_label = "Input CGST"


        # ------------------------------------------------------
        # Amount in words
        # ------------------------------------------------------

        rupees = int(grand_total)

        paise = int(
            round(
                (
                    grand_total -
                    Decimal(rupees)
                ) *
                100
            )
        )

        amount_in_words = (
            "INR "
            + num2words(
                rupees,
                lang="en_IN",
            )
            .title()
        )

        if paise:
            amount_in_words += (
                " And "
                + num2words(
                    paise,
                    lang="en_IN",
                )
                .title()
                + " Paise"
            )

        amount_in_words += " Only"

        # ------------------------------------------------------
        # Other Reference
        # ------------------------------------------------------

        other_reference = (
            f"CFRE / DRONIX "
            f"{purchase_order.po_number} / R0"
        )

        # ------------------------------------------------------
        # Context
        # ------------------------------------------------------

        context = {
            # Fixed Dronix company details
            "company": {
                "name":
                    "Dronix Technologies Private Limited",

                "address_line_1":
                    "No.133, AC Complex, Ground Floor",

                "address_line_2":
                    "Gandhi Road, Alapakkam, Perungalathur",

                "city":
                    "Chennai",

                "gstin":
                    "33AAGCD1081K1ZS",

                "state":
                    "Tamil Nadu",

                "state_code":
                    "33",

                "email":
                    "finance@aero360.co.in",
            },

            # Vendor / Supplier details
            "vendor": {
                "name":
                    (
                        vendor.name
                        if vendor
                        else purchase_order.vendor_name
                    ),

                "address":
                    (
                        vendor.address
                        if vendor
                        else ""
                    ),

                "city":
                    (
                        getattr(
                            vendor,
                            "city",
                            "",
                        )
                        if vendor
                        else ""
                    ),

                "pincode":
                    (
                        getattr(
                            vendor,
                            "pincode",
                            "",
                        )
                        if vendor
                        else ""
                    ),

                "gstin":
                    (
                        vendor.gst_number
                        if vendor
                        else purchase_order.gstin
                    ),

                "state":
                    (
                        getattr(
                            vendor,
                            "state",
                            "",
                        )
                        if vendor
                        else ""
                    ),
"state_code": (
    getattr(
        vendor,
        "state_code",
        "",
    )
    if vendor
    else ""
),
            },

            "purchase_order":
                purchase_order,

            "po_number":
                purchase_order.po_number,

            "reference_number":
                purchase_order.po_number,

            "po_date":
                po_date,

            "other_reference":
                other_reference,

            "items":
                pdf_items,

            "subtotal":
                subtotal,

            "gst_total":
                gst_total,

            "gst_label":
                gst_label,

            "grand_total":
                grand_total,

            "total_quantity":
                total_quantity,

            "amount_in_words":
                amount_in_words,
        }

        # ------------------------------------------------------
        # Render HTML
        # ------------------------------------------------------

        template = get_template(
            "procurement/purchase_order_pdf.html"
        )

        html = template.render(context)

        result = BytesIO()

        pdf_status = pisa.CreatePDF(
            html,
            dest=result,
            encoding="UTF-8",
            link_callback=pdf_link_callback,
        )

        if pdf_status.err:
            return Response(
                {
                    "detail":
                        "Unable to generate Purchase Order PDF."
                },
                status=
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # ------------------------------------------------------
        # Download response
        # ------------------------------------------------------

        safe_po_number = (
            str(purchase_order.po_number)
            .replace("/", "-")
            .replace("\\", "-")
        )

        response = HttpResponse(
            result.getvalue(),
            content_type="application/pdf",
        )

        response[
            "Content-Disposition"
        ] = (
            f'attachment; '
            f'filename="PO_{safe_po_number}.pdf"'
        )

        return response
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
                ),
                order_type="STANDARD",
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
    @transaction.atomic
    def sync_material_request_po_progress(
        self,
        source_mr_number,
    ):
        """
        Synchronize the NORMAL procurement shortage without counting QC
        replacement POs as additional demand. Replacement orders have their
        own MR workflow statuses and are evaluated separately.
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

        all_related_purchase_orders = list(
            PurchaseOrder.objects
            .select_for_update()
            .filter(source_mr_number=canonical_mr_number)
            .exclude(
                status__in=[
                    "REJECTED",
                    "FINANCE_REJECTED",
                ]
            )
        )

        standard_purchase_orders = [
            po
            for po in all_related_purchase_orders
            if str(getattr(po, "order_type", "STANDARD") or "STANDARD")
            .strip()
            .upper()
            != "REPLACEMENT"
        ]

        replacement_purchase_orders = [
            po
            for po in all_related_purchase_orders
            if str(getattr(po, "order_type", "STANDARD") or "STANDARD")
            .strip()
            .upper()
            == "REPLACEMENT"
        ]

        standard_po_ids = [po.id for po in standard_purchase_orders]

        quantity_rows = (
            PurchaseOrderItem.objects
            .filter(purchase_order_id__in=standard_po_ids)
            .values("component_id")
            .annotate(
                ordered_quantity=Sum("quantity"),
                delivered_quantity=Sum("received_quantity"),
            )
        )

        component_progress = {
            int(row["component_id"]): {
                "ordered_quantity": int(row["ordered_quantity"] or 0),
                "delivered_quantity": int(row["delivered_quantity"] or 0),
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
                {"ordered_quantity": 0, "delivered_quantity": 0},
            )
            ordered_quantity = int(progress["ordered_quantity"])
            delivered_quantity = int(progress["delivered_quantity"])
            shortage_quantity = int(group["shortage_quantity"] or 0)

            ordered_distribution = self.distribute_quantity(
                group["items"],
                ordered_quantity,
            )
            delivered_distribution = self.distribute_quantity(
                group["items"],
                delivered_quantity,
            )

            for request_item in group["items"]:
                changed_fields = []
                item_ordered = ordered_distribution.get(request_item.pk, 0)
                item_delivered = delivered_distribution.get(request_item.pk, 0)

                if int(request_item.po_raised_quantity or 0) != item_ordered:
                    request_item.po_raised_quantity = item_ordered
                    changed_fields.append("po_raised_quantity")

                if int(request_item.delivered_quantity or 0) != item_delivered:
                    request_item.delivered_quantity = item_delivered
                    changed_fields.append("delivered_quantity")

                if changed_fields:
                    request_item.save(update_fields=changed_fields)

            if shortage_quantity > 0:
                shortage_components.append(
                    {
                        "component_id": component_id,
                        "shortage_quantity": shortage_quantity,
                        "ordered_quantity": ordered_quantity,
                        "delivered_quantity": delivered_quantity,
                    }
                )

        current_status = str(material_request.status or "").strip().upper()
        if current_status in {
            "QC_CHECKED",
            "PROJECT_INVENTORY_READY",
            "INVENTORY_ISSUED",
            "MR_COMPLETED",
        }:
            return material_request

        # Replacement stage takes precedence over the original PO delivery
        # stage. This is what keeps an MR open while replacement material is
        # awaiting approval, ordering, delivery or QC.
        replacement_status = self.get_replacement_mr_status(
            replacement_purchase_orders
        )
        if replacement_status:
            if replacement_status != current_status:
                material_request.status = replacement_status
                material_request.po_raised = True
                material_request.save(
                    update_fields=["status", "po_raised"]
                )
            return material_request

        # An MR with no Procurement shortage belongs only to Inventory.
        if not shortage_components:
            return material_request

        all_shortages_have_po = all(
            row["ordered_quantity"] >= row["shortage_quantity"]
            for row in shortage_components
        )
        standard_po_exists = bool(standard_purchase_orders)
        all_standard_pos_delivered = (
            standard_po_exists
            and all(
                str(po.status or "").strip().upper() == "DELIVERED"
                for po in standard_purchase_orders
            )
        )
        all_shortages_delivered = (
            all_shortages_have_po
            and all(
                row["delivered_quantity"] >= row["shortage_quantity"]
                for row in shortage_components
            )
        )
        all_delivered = all_standard_pos_delivered and all_shortages_delivered
        any_shortage_delivered = any(
            int(row["delivered_quantity"] or 0) > 0
            for row in shortage_components
        )

        reference_id = str(material_request.id)

        if all_delivered:
            material_request.status = "PO_DELIVERED"
            material_request.po_raised = True
            material_request.save(update_fields=["status", "po_raised"])
            Notification.objects.filter(
                category="MR",
                receiver="PROCUREMENT",
                reference_id=reference_id,
            ).update(
                status="PO_DELIVERED",
                is_read=True,
                message=(
                    "All reserved Procurement shortages were delivered for "
                    f"{material_request.material_request_id}."
                ),
            )
        elif any_shortage_delivered:
            material_request.status = "PARTIALLY_DELIVERED"
            material_request.po_raised = True
            material_request.save(update_fields=["status", "po_raised"])
            Notification.objects.filter(
                category="MR",
                receiver="PROCUREMENT",
                reference_id=reference_id,
            ).update(
                status="PARTIALLY_DELIVERED",
                is_read=False,
                message=(
                    "Part of the Procurement shortage has been delivered for "
                    f"{material_request.material_request_id}. Remaining "
                    "components or quantities are still awaiting delivery."
                ),
            )
        elif all_shortages_have_po:
            material_request.status = "PO_RAISED"
            material_request.po_raised = True
            material_request.save(update_fields=["status", "po_raised"])
            Notification.objects.filter(
                category="MR",
                receiver="PROCUREMENT",
                reference_id=reference_id,
            ).update(
                status="PO_RAISED",
                is_read=True,
                message=(
                    "Purchase Orders cover every reserved shortage for "
                    f"{material_request.material_request_id}."
                ),
            )
        else:
            material_request.status = "PROCUREMENT_PENDING"
            material_request.po_raised = False
            material_request.save(update_fields=["status", "po_raised"])
            Notification.objects.filter(
                category="MR",
                receiver="PROCUREMENT",
                reference_id=reference_id,
            ).update(
                status="PROCUREMENT_PENDING",
                is_read=False,
                message=(
                    "Additional Purchase Orders are still required for the "
                    "reserved shortage of "
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

            # Return a Procurement progress email to the original
            # MR requester/Engineer after this linked PO commits.
            transaction.on_commit(
                lambda po_id=purchase_order.id: (
                    self.send_mr_requester_po_raised_email(
                        po_id
                    )
                )
            )

        # Finance email for BOTH Direct PO and MR-linked PO.
        create_approval_status = str(
            purchase_order.approval_status
            or ""
        ).strip().upper()

        create_status = str(
            purchase_order.status
            or ""
        ).strip().upper()

        if (
            create_approval_status
            == "PENDING_FINANCE"
            or create_status
            == "PENDING_FINANCE"
        ):
            # Store exactly who raised/created this PO before
            # Finance receives it. Works for Direct PO and MR PO.
            self.save_finance_notification_with_sender(
                purchase_order
            )

            transaction.on_commit(
                lambda po_id=purchase_order.id: (
                    self.send_finance_approval_email(
                        po_id
                    )
                )
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

        old_status = str(
            serializer.instance.status
            or ""
        ).upper()

        purchase_order = serializer.save()

        new_status = str(
            purchase_order.status
            or ""
        ).upper()

        if (
            self.is_direct_standard_po(
                purchase_order
            )
            and new_status == "ORDERED"
            and old_status != "APPROVED"
        ):
            raise ValidationError(
                {
                    "status": (
                        "Manager approval is required "
                        "before a Direct PO can be "
                        "marked Ordered."
                    )
                }
            )

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
            # Preserve the original Procurement sender.
            # Do not delete/recreate the Finance notification because
            # that would erase requested_by.
            self.save_finance_notification_with_sender(
                purchase_order
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

            transaction.on_commit(
                lambda po_id=purchase_order.id: (
                    self.send_finance_approval_email(
                        po_id
                    )
                )
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

            # Finance approval is NOT the final approval for a Direct PO.
            # Direct PO: notify/email Manager for final approval.
            # MR-linked standard PO: preserve the existing Finance-only flow.
            if self.is_direct_standard_po(
                purchase_order
            ):
                self.save_direct_po_manager_notification(
                    purchase_order
                )

                transaction.on_commit(
                    lambda po_id=purchase_order.id: (
                        self.send_direct_po_manager_approval_email(
                            po_id
                        )
                    )
                )
            else:
                # Existing MR-linked PO behavior remains unchanged.
                transaction.on_commit(
                    lambda po_id=purchase_order.id: (
                        self.send_po_requester_result_email(
                            po_id,
                            outcome="approved",
                        )
                    )
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

            # Return Finance rejection result to the Procurement
            # user who originally sent this PO for approval.
            transaction.on_commit(
                lambda po_id=purchase_order.id: (
                    self.send_po_requester_result_email(
                        po_id,
                        outcome="rejected",
                    )
                )
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

    @action(
        detail=True,
        methods=["post"],
        url_path="direct-manager-approve",
    )
    @transaction.atomic
    def direct_manager_approve(
        self,
        request,
        pk=None,
    ):
        """
        Final Manager approval for a Direct STANDARD PO.

        Finance must approve first. No Manager approval is required
        for MR-linked standard POs or QC Replacement POs.
        """
        self.require_active_role(
            request,
            "manager",
            "admin",
        )

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
                status=status.HTTP_404_NOT_FOUND,
            )

        if not self.is_direct_standard_po(
            purchase_order
        ):
            return Response(
                {
                    "detail": (
                        "Manager approval through this "
                        "action is only for Direct POs."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            str(
                purchase_order.status
                or ""
            ).upper()
            != "FINANCE_APPROVED"
        ):
            return Response(
                {
                    "detail": (
                        "Finance must approve this Direct "
                        "PO before Manager approval."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = self.get_request_actor_name(
            request
        )

        purchase_order.status = "APPROVED"
        purchase_order.approved_by = actor
        purchase_order.approved_at = timezone.now()
        purchase_order.rejection_reason = None
        purchase_order.rejected_by = None

        purchase_order.save(
            update_fields=[
                "status",
                "approved_by",
                "approved_at",
                "rejection_reason",
                "rejected_by",
            ]
        )

        Notification.objects.filter(
            category="PO",
            receiver="MANAGER",
            reference_id=str(
                purchase_order.id
            ),
        ).update(
            status="APPROVED",
            is_read=True,
        )

        return Response(
            self.get_serializer(
                purchase_order
            ).data
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="direct-manager-reject",
    )
    @transaction.atomic
    def direct_manager_reject(
        self,
        request,
        pk=None,
    ):
        """
        Manager rejects a Direct STANDARD PO after Finance approval.

        This action is ONLY for Direct POs.
        QC Replacement POs remain Procurement-approved in the PO table.
        """
        self.require_active_role(
            request,
            "manager",
            "admin",
        )

        reason = str(
            request.data.get("reason")
            or request.data.get("remarks")
            or request.data.get("rejection_reason")
            or ""
        ).strip()

        if not reason:
            return Response(
                {
                    "detail":
                        "Manager rejection reason is required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

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
                status=status.HTTP_404_NOT_FOUND,
            )

        if not self.is_direct_standard_po(
            purchase_order
        ):
            return Response(
                {
                    "detail": (
                        "Manager rejection through this "
                        "action is only for Direct POs."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            str(
                purchase_order.status
                or ""
            ).upper()
            != "FINANCE_APPROVED"
        ):
            return Response(
                {
                    "detail": (
                        "This Direct PO is not pending "
                        "Manager approval."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = self.get_request_actor_name(
            request
        )

        purchase_order.status = "REJECTED"
        purchase_order.approval_status = "REJECTED"
        purchase_order.rejection_reason = reason
        purchase_order.rejected_by = actor

        purchase_order.save(
            update_fields=[
                "status",
                "approval_status",
                "rejection_reason",
                "rejected_by",
            ]
        )

        Notification.objects.filter(
            category="PO",
            receiver="MANAGER",
            reference_id=str(
                purchase_order.id
            ),
        ).update(
            status="REJECTED",
            is_read=True,
        )

        return Response(
            self.get_serializer(
                purchase_order
            ).data
        )


    # ==========================================================
    # QC REPLACEMENT APPROVAL ACTIONS
    #
    # Replacement flow:
    # Procurement approval only -> Replacement Approved -> Ordered.
    #
    # Historical Manager/Finance replacement endpoints below remain
    # available only for old database rows.
    # ==========================================================

    @action(
        detail=True,
        methods=["post"],
        url_path="replacement-procurement-approve",
    )
    @transaction.atomic
    def replacement_procurement_approve(self, request, pk=None):
        """
        Procurement approves a newly raised QC Replacement PO.

        No Manager/Finance email or notification is created here.
        """
        self.require_active_role(
            request,
            "procurement",
            "admin",
        )

        purchase_order, error_response = (
            self.get_replacement_po_or_error(pk)
        )

        if error_response:
            return error_response

        if (
            str(purchase_order.status or "").upper()
            != "REPLACEMENT_PENDING_MANAGER"
        ):
            return Response(
                {
                    "detail": (
                        "This Replacement PO is not "
                        "pending Procurement approval."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = self.get_request_actor_name(request)

        purchase_order.status = "REPLACEMENT_APPROVED"
        purchase_order.approval_status = "NOT_REQUESTED"
        purchase_order.approved_by = actor
        purchase_order.approved_at = timezone.now()
        purchase_order.rejection_reason = None
        purchase_order.rejected_by = None

        purchase_order.save(
            update_fields=[
                "status",
                "approval_status",
                "approved_by",
                "approved_at",
                "rejection_reason",
                "rejected_by",
            ]
        )

        # Defensive cleanup. The new replacement flow must not leave
        # Manager/Finance approval notifications behind.
        Notification.objects.filter(
            category="PO",
            receiver__in=["MANAGER", "FINANCE"],
            reference_id=str(purchase_order.id),
        ).delete()

        self.sync_replacement_mr_status(
            purchase_order
        )

        return Response(
            self.get_serializer(
                purchase_order
            ).data
        )


    @action(
        detail=True,
        methods=["post"],
        url_path="replacement-manager-approve",
    )
    @transaction.atomic
    def replacement_manager_approve(self, request, pk=None):
        self.require_active_role(request, "manager", "admin")
        purchase_order, error_response = self.get_replacement_po_or_error(pk)
        if error_response:
            return error_response

        if str(purchase_order.status or "").upper() != "REPLACEMENT_PENDING_MANAGER":
            return Response(
                {"detail": "This replacement is not pending Manager approval."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = self.get_request_actor_name(request)
        original_sender = (
            Notification.objects
            .filter(
                category="PO",
                receiver="MANAGER",
                reference_id=str(purchase_order.id),
            )
            .exclude(requested_by__isnull=True)
            .exclude(requested_by="")
            .values_list("requested_by", flat=True)
            .first()
            or ""
        )

        purchase_order.status = "REPLACEMENT_PENDING_FINANCE"
        purchase_order.approval_status = "REPLACEMENT_PENDING_FINANCE"
        purchase_order.rejection_reason = None
        purchase_order.rejected_by = None
        purchase_order.save(
            update_fields=[
                "status",
                "approval_status",
                "rejection_reason",
                "rejected_by",
            ]
        )

        PurchaseOrderApproval.objects.create(
            purchase_order=purchase_order,
            action="REPLACEMENT_MANAGER_APPROVED",
            requested_by=str(original_sender or actor)[:100],
            approved_by=actor,
        )

        Notification.objects.filter(
            category="PO",
            receiver="MANAGER",
            reference_id=str(purchase_order.id),
        ).update(
            status="REPLACEMENT_MANAGER_APPROVED",
            is_read=True,
        )

        self.save_replacement_notification(
            purchase_order,
            receiver="FINANCE",
            notification_status="REPLACEMENT_PENDING_FINANCE",
            title=f"Replacement PO Finance Approval - {purchase_order.po_number}",
            message=(
                f"Manager approved QC replacement PO {purchase_order.po_number}. "
                "Finance approval is now required."
            ),
            requested_by=original_sender or actor,
        )

        self.sync_replacement_mr_status(purchase_order)
        return Response(self.get_serializer(purchase_order).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="replacement-manager-reject",
    )
    @transaction.atomic
    def replacement_manager_reject(self, request, pk=None):
        self.require_active_role(request, "manager", "admin")
        purchase_order, error_response = self.get_replacement_po_or_error(pk)
        if error_response:
            return error_response

        if str(purchase_order.status or "").upper() != "REPLACEMENT_PENDING_MANAGER":
            return Response(
                {"detail": "This replacement is not pending Manager approval."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = str(
            request.data.get("reason")
            or request.data.get("remarks")
            or ""
        ).strip()
        if not reason:
            return Response(
                {"detail": "Rejection reason is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = self.get_request_actor_name(request)
        purchase_order.status = "REPLACEMENT_MANAGER_REJECTED"
        purchase_order.approval_status = "REPLACEMENT_MANAGER_REJECTED"
        purchase_order.rejection_reason = reason
        purchase_order.rejected_by = actor
        purchase_order.save(
            update_fields=[
                "status",
                "approval_status",
                "rejection_reason",
                "rejected_by",
            ]
        )

        PurchaseOrderApproval.objects.create(
            purchase_order=purchase_order,
            action="REPLACEMENT_MANAGER_REJECTED",
            requested_by=actor,
            approved_by=actor,
            finance_remarks=reason,
        )

        Notification.objects.filter(
            category="PO",
            receiver="MANAGER",
            reference_id=str(purchase_order.id),
        ).update(
            status="REPLACEMENT_MANAGER_REJECTED",
            is_read=True,
        )

        self.sync_replacement_mr_status(purchase_order)
        return Response(self.get_serializer(purchase_order).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="replacement-finance-approve",
    )
    @transaction.atomic
    def replacement_finance_approve(self, request, pk=None):
        self.require_active_role(request, "finance", "admin")
        purchase_order, error_response = self.get_replacement_po_or_error(pk)
        if error_response:
            return error_response

        if str(purchase_order.status or "").upper() != "REPLACEMENT_PENDING_FINANCE":
            return Response(
                {"detail": "This replacement is not pending Finance approval."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = self.get_request_actor_name(request)
        remarks = str(
            request.data.get("remarks")
            or request.data.get("finance_remarks")
            or ""
        ).strip()

        purchase_order.status = "REPLACEMENT_APPROVED"
        purchase_order.approval_status = "REPLACEMENT_FINANCE_APPROVED"
        purchase_order.finance_remarks = remarks or purchase_order.finance_remarks
        purchase_order.approved_by = actor
        purchase_order.approved_at = timezone.now()
        purchase_order.rejection_reason = None
        purchase_order.rejected_by = None
        purchase_order.save(
            update_fields=[
                "status",
                "approval_status",
                "finance_remarks",
                "approved_by",
                "approved_at",
                "rejection_reason",
                "rejected_by",
            ]
        )

        PurchaseOrderApproval.objects.create(
            purchase_order=purchase_order,
            action="REPLACEMENT_FINANCE_APPROVED",
            requested_by=actor,
            approved_by=actor,
            finance_remarks=remarks or None,
        )

        Notification.objects.filter(
            category="PO",
            receiver="FINANCE",
            reference_id=str(purchase_order.id),
        ).update(
            status="REPLACEMENT_FINANCE_APPROVED",
            is_read=True,
        )

        self.sync_replacement_mr_status(purchase_order)
        return Response(self.get_serializer(purchase_order).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="replacement-finance-reject",
    )
    @transaction.atomic
    def replacement_finance_reject(self, request, pk=None):
        self.require_active_role(request, "finance", "admin")
        purchase_order, error_response = self.get_replacement_po_or_error(pk)
        if error_response:
            return error_response

        if str(purchase_order.status or "").upper() != "REPLACEMENT_PENDING_FINANCE":
            return Response(
                {"detail": "This replacement is not pending Finance approval."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = str(
            request.data.get("reason")
            or request.data.get("remarks")
            or ""
        ).strip()
        if not reason:
            return Response(
                {"detail": "Rejection reason is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = self.get_request_actor_name(request)
        purchase_order.status = "REPLACEMENT_FINANCE_REJECTED"
        purchase_order.approval_status = "REPLACEMENT_FINANCE_REJECTED"
        purchase_order.finance_remarks = reason
        purchase_order.rejection_reason = reason
        purchase_order.rejected_by = actor
        purchase_order.save(
            update_fields=[
                "status",
                "approval_status",
                "finance_remarks",
                "rejection_reason",
                "rejected_by",
            ]
        )

        PurchaseOrderApproval.objects.create(
            purchase_order=purchase_order,
            action="REPLACEMENT_FINANCE_REJECTED",
            requested_by=actor,
            approved_by=actor,
            finance_remarks=reason,
        )

        Notification.objects.filter(
            category="PO",
            receiver="FINANCE",
            reference_id=str(purchase_order.id),
        ).update(
            status="REPLACEMENT_FINANCE_REJECTED",
            is_read=True,
        )

        self.sync_replacement_mr_status(purchase_order)
        return Response(self.get_serializer(purchase_order).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="replacement-mark-ordered",
    )
    @transaction.atomic
    def replacement_mark_ordered(self, request, pk=None):
        self.require_active_role(request, "procurement", "admin")
        purchase_order, error_response = self.get_replacement_po_or_error(pk)
        if error_response:
            return error_response

        if str(purchase_order.status or "").upper() != "REPLACEMENT_APPROVED":
            return Response(
                {"detail": "The Replacement PO must be approved before it can be ordered."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actor = self.get_request_actor_name(request)
        purchase_order.status = "REPLACEMENT_ORDERED"
        purchase_order.save(update_fields=["status"])

        PurchaseOrderApproval.objects.create(
            purchase_order=purchase_order,
            action="REPLACEMENT_ORDERED",
            requested_by=actor,
            approved_by=actor,
        )

        self.sync_replacement_mr_status(purchase_order)
        return Response(self.get_serializer(purchase_order).data)


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
            "REPLACEMENT_ORDERED",
            "REPLACEMENT_PARTIALLY_RECEIVED",
        }

        if current_status not in allowed_statuses:
            return Response(
                {
                    "detail": (
                        "Only an ordered Purchase Order can receive "
                        "material. Standard POs must be ORDERED or "
                        "PARTIALLY_DELIVERED; replacement POs must be "
                        "REPLACEMENT_ORDERED or "
                        "REPLACEMENT_PARTIALLY_RECEIVED."
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

        is_replacement = (
            str(
                getattr(
                    purchase_order,
                    "order_type",
                    "STANDARD",
                )
                or "STANDARD"
            ).strip().upper()
            == "REPLACEMENT"
        )

        if is_replacement:
            purchase_order.status = (
                "REPLACEMENT_PARTIALLY_RECEIVED"
                if has_remaining_quantity
                else "REPLACEMENT_RECEIVED"
            )
        else:
            purchase_order.status = (
                "PARTIALLY_DELIVERED"
                if has_remaining_quantity
                else "DELIVERED"
            )

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