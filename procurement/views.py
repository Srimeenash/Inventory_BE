import re
from urllib.parse import unquote, urlparse
from django.db import IntegrityError, transaction
from django.db.models import F, Prefetch, Q, Sum
from django.utils import timezone

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.contrib.staticfiles import finders
from inventory.models import InventoryReservation
from components.models import Component
from materialrequest.models import MaterialRequest
from notifications.models import Notification
from outward.models import OutwardEntry
from notifications.email_service import send_ipms_email
from users.models import User
from django.conf import settings
from django.core.cache import cache
from decimal import Decimal
from io import BytesIO

from django.http import HttpResponse
from django.template.loader import get_template

from num2words import num2words
from xhtml2pdf import pisa

from vendors.models import Vendor
from inventory_backend.cache_utils import (
    build_list_cache_key,
    get_cache_version,
    invalidate_cache_version,
)
from inventory_backend.pagination import (
    OptionalPageNumberPagination,
    apply_server_query_parameters,
)
from .models import (
    PurchaseOrder,
    PurchaseOrderApproval,
    PurchaseOrderItem,
    PurchaseOrderPdfVoucher,
    PurchaseRequest,
)
from .serializers import (
    PurchaseOrderSerializer,
    PurchaseRequestSerializer,
)
from pathlib import Path


PURCHASE_REQUEST_LIST_CACHE_TTL_SECONDS = 60
PURCHASE_REQUEST_LIST_CACHE_VERSION_KEY = "ipms:purchase_requests:list:version"
PURCHASE_ORDER_LIST_CACHE_TTL_SECONDS = 60
PURCHASE_ORDER_LIST_CACHE_VERSION_KEY = "ipms:purchase_orders:list:version"
NOTIFICATION_LIST_CACHE_VERSION_KEY = "ipms:notifications:list:version"


def get_purchase_request_cache_version():
    return get_cache_version(PURCHASE_REQUEST_LIST_CACHE_VERSION_KEY)


def invalidate_purchase_request_cache():
    invalidate_cache_version(PURCHASE_REQUEST_LIST_CACHE_VERSION_KEY)


def get_purchase_order_cache_version():
    return get_cache_version(PURCHASE_ORDER_LIST_CACHE_VERSION_KEY)


def invalidate_purchase_order_cache():
    invalidate_cache_version(PURCHASE_ORDER_LIST_CACHE_VERSION_KEY)


def invalidate_po_notification_cache():
    """Refresh Manager/Finance notification lists immediately."""
    invalidate_cache_version(NOTIFICATION_LIST_CACHE_VERSION_KEY)


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

    # ----------------------------------------------------------
    # Resolve Django static files for xhtml2pdf.
    #
    # Example:
    #   /static/images/aero360_logo.png
    #       ->
    #   C:\\...\\project\\static\\images\\aero360_logo.png
    # ----------------------------------------------------------
    static_url = str(
        getattr(settings, "STATIC_URL", "/static/")
        or "/static/"
    )

    # Normalize both "/static/..." and "static/..." forms.
    normalized_uri = unquote(str(uri or "")).replace("\\", "/")
    normalized_static_url = static_url.replace("\\", "/")

    if not normalized_static_url.startswith("/"):
        normalized_static_url = "/" + normalized_static_url

    if not normalized_static_url.endswith("/"):
        normalized_static_url += "/"

    parsed_uri = urlparse(normalized_uri)
    if parsed_uri.scheme == "file":
        file_path = Path(parsed_uri.path)
        if file_path.exists():
            return str(file_path.resolve())

    direct_path = Path(normalized_uri)
    if direct_path.exists():
        return str(direct_path.resolve())

    candidate_uri = parsed_uri.path or normalized_uri
    if candidate_uri.startswith("static/"):
        candidate_uri = "/" + candidate_uri

    if candidate_uri.startswith(normalized_static_url):
        relative_path = candidate_uri[
            len(normalized_static_url):
        ].lstrip("/")

        # First use Django's staticfiles finders.
        static_path = finders.find(relative_path)
        if static_path:
            return str(static_path)

        # Robust fallback for this project's source static folder:
        #     BASE_DIR/static/images/aero360_logo.png
        direct_static_path = (
            Path(settings.BASE_DIR)
            / "static"
            / Path(relative_path)
        )

        if direct_static_path.exists():
            return str(direct_static_path.resolve())

    # xhtml2pdf may pass the path without Django's STATIC_URL prefix.
    relative_static_path = (
        Path(settings.BASE_DIR)
        / "static"
        / candidate_uri.lstrip("/")
    )
    if relative_static_path.exists():
        return str(relative_static_path.resolve())

    return uri
class PurchaseRequestViewSet(viewsets.ModelViewSet):
    queryset = (
        PurchaseRequest.objects
        .all()
        .order_by("-created_at")
    )
    serializer_class = PurchaseRequestSerializer
    permission_classes = [AllowAny]
    pagination_class = OptionalPageNumberPagination

    def list(self, request, *args, **kwargs):
        version = get_purchase_request_cache_version()
        cache_key = None

        if version:
            cache_key = build_list_cache_key(
                "ipms:purchase_requests:list",
                version,
                request,
            )
            try:
                cached_data = cache.get(cache_key)
            except Exception:
                cached_data = None
            if cached_data is not None:
                return Response(cached_data)

        response = super().list(request, *args, **kwargs)

        if cache_key and response.status_code == 200:
            try:
                cache.set(
                    cache_key,
                    response.data,
                    timeout=PURCHASE_REQUEST_LIST_CACHE_TTL_SECONDS,
                )
            except Exception:
                pass

        return response

    def get_queryset(self):
        return apply_server_query_parameters(
            super().get_queryset(),
            self.request,
            search_fields=(
                "pr_number",
                "requested_by",
                "department",
                "remarks",
                "status",
            ),
            filter_fields={
                "status": "status__iexact",
                "department": "department__icontains",
            },
            ordering_fields=("pr_number", "created_at", "status"),
            default_ordering=("-created_at", "-id"),
        )

    @transaction.atomic
    def perform_create(self, serializer):
        invalidate_purchase_request_cache()
        return super().perform_create(serializer)

    @transaction.atomic
    def perform_update(self, serializer):
        invalidate_purchase_request_cache()
        return super().perform_update(serializer)


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    queryset = (
        PurchaseOrder.objects
        .prefetch_related(
            Prefetch(
                "items",
                queryset=(
                    PurchaseOrderItem.objects
                    .select_related("component")
                ),
            ),
        )
        .all()
        .order_by("-created_at")
    )

    serializer_class = PurchaseOrderSerializer
    pagination_class = OptionalPageNumberPagination

    # Parse JWT when the caller sends one, but preserve the
    # existing AllowAny behavior for routes that still rely on it.
    authentication_classes = [JWTAuthentication]
    permission_classes = [AllowAny]

    def list(self, request, *args, **kwargs):
        version = get_purchase_order_cache_version()
        cache_key = None

        if version:
            cache_key = build_list_cache_key(
                "ipms:purchase_orders:list",
                version,
                request,
            )
            try:
                cached_data = cache.get(cache_key)
            except Exception:
                cached_data = None
            if cached_data is not None:
                return Response(cached_data)

        response = super().list(request, *args, **kwargs)

        if cache_key and response.status_code == 200:
            try:
                cache.set(
                    cache_key,
                    response.data,
                    timeout=PURCHASE_ORDER_LIST_CACHE_TTL_SECONDS,
                )
            except Exception:
                pass

        return response

    def get_queryset(self):
        return apply_server_query_parameters(
            super().get_queryset(),
            self.request,
            search_fields=(
                "po_number",
                "vendor_name",
                "gstin",
                "location",
                "status",
                "approval_status",
                "source_mr_number",
                "remarks",
            ),
            filter_fields={
                "status": "status__iexact",
                "approval_status": "approval_status__iexact",
                "vendor_name": "vendor_name__icontains",
                "source_mr_number": "source_mr_number__icontains",
            },
            ordering_fields=(
                "po_number",
                "vendor_name",
                "ordered_date",
                "expected_delivery_date",
                "created_at",
                "status",
            ),
            default_ordering=("-created_at", "-id"),
        )

    @action(
        detail=False,
        methods=["get"],
        url_path="next-number",
    )
    def next_number(self, request):
        current_date = timezone.localdate()
        start_year = (
            current_date.year
            if current_date.month >= 4
            else current_date.year - 1
        )
        financial_year = (
            f"{str(start_year)[-2:]}-"
            f"{str(start_year + 1)[-2:]}"
        )

        highest_sequence = 0
        for value in PurchaseOrder.objects.values_list(
            "po_number",
            flat=True,
        ):
            match = re.match(
                r"^(\d+)/(\d{2}-\d{2})$",
                str(value or "").strip(),
            )

            if not match or match.group(2) != financial_year:
                continue

            highest_sequence = max(
                highest_sequence,
                int(match.group(1)),
            )

        return Response(
            {
                "po_number": (
                    f"{highest_sequence + 1:02d}/"
                    f"{financial_year}"
                )
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=False,
        methods=["get"],
        url_path="last-unit-price",
    )
    def last_unit_price(self, request):
        """
        Return the most recent valid PO unit price and its vendor for one component.

        Used by Create PO:
        selecting a component from a later PO automatically suggests
        the last purchase price in Unit Price only.

        Other values such as GST, Discount, Freight Cost, Freight GST,
        UOM and Round-Off are NOT copied from the previous PO.
        """
        component_id = request.query_params.get(
            "component_id"
        )

        if not component_id:
            return Response(
                {
                    "detail": (
                        "component_id query parameter "
                        "is required."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            component_id = int(component_id)
        except (TypeError, ValueError):
            return Response(
                {
                    "detail": (
                        "component_id must be a valid "
                        "Component database ID."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        component = (
            Component.objects
            .filter(pk=component_id)
            .first()
        )

        if component is None:
            return Response(
                {"detail": "Component not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        rejected_po_statuses = {
            "REJECTED",
            "FINANCE_REJECTED",
            "REPLACEMENT_MANAGER_REJECTED",
            "REPLACEMENT_FINANCE_REJECTED",
        }

        last_item = (
            PurchaseOrderItem.objects
            .select_related(
                "purchase_order",
                "component",
            )
            .filter(component_id=component_id)
            .exclude(
                purchase_order__status__in=(
                    rejected_po_statuses
                )
            )
            .order_by(
                "-purchase_order__created_at",
                "-id",
            )
            .first()
        )

        if last_item is None:
            return Response(
                {
                    "component_id": component_id,
                    "component_code": (
                        component.component_id
                    ),
                    "unit_price": None,
                    "vendor_name": "",
                    "po_number": "",
                    "has_previous_purchase": False,
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            {
                "component_id": component_id,
                "component_code": (
                    component.component_id
                ),
                "unit_price": str(
                    last_item.unit_price
                ),
                "po_number": (
                    last_item
                    .purchase_order
                    .po_number
                ),
                "vendor_name": (
                    last_item.purchase_order.vendor_name or ""
                ),
                "purchase_order_id": (
                    last_item.purchase_order_id
                ),
                "has_previous_purchase": True,
            },
            status=status.HTTP_200_OK,
        )

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

    @staticmethod
    def get_returnable_restore_outward_id(
        purchase_order,
    ):
        """
        Returnable Restore replacement POs carry:

            RETURNABLE_RESTORE_OUTWARD:<outward_id>

        in PO remarks.
        """
        if purchase_order is None:
            return None

        remarks = str(
            getattr(
                purchase_order,
                "remarks",
                "",
            )
            or ""
        )

        match = re.search(
            r"RETURNABLE_RESTORE_OUTWARD:(\d+)",
            remarks,
            flags=re.IGNORECASE,
        )

        if not match:
            return None

        try:
            return int(
                match.group(1)
            )
        except (
            TypeError,
            ValueError,
        ):
            return None

    @classmethod
    def sync_returnable_restore_outward_status(
        cls,
        purchase_order,
        restore_status,
    ):
        """
        Keep Outward -> Failed QC Restore status synchronized with the
        replacement PO lifecycle.

        This is UI/audit synchronization only. Physical stock movement is
        handled by Inward QC:
            Restore delivery QC PASS -> Central In Store.
        """
        outward_id = (
            cls.get_returnable_restore_outward_id(
                purchase_order
            )
        )

        if not outward_id:
            return None

        outward = (
            OutwardEntry.objects
            .select_for_update()
            .filter(
                pk=outward_id
            )
            .first()
        )

        if outward is None:
            return None

        metadata = (
            outward.inventory_allocations
            if isinstance(
                outward.inventory_allocations,
                dict,
            )
            else {}
        )

        metadata[
            "procurement_restore_status"
        ] = str(
            restore_status
            or ""
        ).strip().upper()

        metadata[
            "restore_last_po_id"
        ] = purchase_order.pk

        metadata[
            "restore_last_po_number"
        ] = purchase_order.po_number

        metadata[
            "restore_last_po_status"
        ] = purchase_order.status

        outward.inventory_allocations = (
            metadata
        )

        status_map = {
            "FINANCE_APPROVED":
                "RESTORE_FINANCE_APPROVED",
            "FINANCE_REJECTED":
                "RESTORE_FINANCE_REJECTED",
            "ORDERED":
                "RESTORE_ORDERED",
        }

        mapped_status = status_map.get(
            str(
                restore_status
                or ""
            )
            .strip()
            .upper()
        )

        if mapped_status:
            outward.status = (
                mapped_status
            )

        outward.save(
            update_fields=[
                "inventory_allocations",
                "status",
                "updated_at",
            ]
        )

        return outward


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
                .prefetch_related(
                    Prefetch(
                        "items",
                        queryset=(
                            PurchaseOrderItem.objects
                            .select_related("component")
                        ),
                    ),
                )
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
        requested_by_override="",
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

        # Direct PO Manager approval happens under the Manager JWT.
        # For that transition, requested_by_override carries the ORIGINAL
        # Procurement/Admin user who created the Direct PO.
        actor_name = (
            self.get_authenticated_po_sender_name()
        )

        requested_by = (
            str(
                requested_by_override
                or ""
            ).strip()
            or actor_name
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

            invalidate_po_notification_cache()
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

        notification = Notification.objects.create(
            **create_kwargs
        )
        invalidate_po_notification_cache()
        return notification


    # ==========================================================
    # DIRECT PO - MANAGER APPROVAL
    # Manager -> Finance -> Procurement can order
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
        Create exactly one Manager notification immediately after a
        Direct STANDARD PO is created.

        Direct PO approval order:
            Create -> Manager -> Finance -> Ordered -> Delivery
        """
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

        # Preserve the original PO creator if this notification already
        # exists; otherwise use the authenticated creator of the Direct PO.
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

        requested_by = str(
            preserved_sender
            or self.get_authenticated_po_sender_name()
            or ""
        ).strip()[:150]

        notification = queryset.first()

        title = (
            "Direct PO Manager Approval - "
            f"{purchase_order.po_number}"
        )

        message = (
            f"Direct PO {purchase_order.po_number} "
            "has been created. Manager approval is required "
            "before this PO is sent to Finance."
        )

        if notification:
            notification.title = title
            notification.message = message
            notification.status = "PENDING_MANAGER"
            notification.receiver = "MANAGER"
            notification.is_read = False

            if requested_by:
                notification.requested_by = requested_by

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

            invalidate_po_notification_cache()
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

        notification = Notification.objects.create(
            **create_kwargs
        )
        invalidate_po_notification_cache()
        return notification

    def send_direct_po_manager_approval_email(
        self,
        purchase_order_id,
    ):
        """
        Email all active Manager users immediately after a Direct
        STANDARD PO is created. Manager approval is the first stage.
        """
        try:
            purchase_order = (
                PurchaseOrder.objects
                .prefetch_related(
                    Prefetch(
                        "items",
                        queryset=(
                            PurchaseOrderItem.objects
                            .select_related("component")
                        ),
                    ),
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
            f"Direct Purchase Order {purchase_order.po_number} "
            "has been created and is waiting for Manager approval. "
            "Finance will receive this PO only after Manager approval."
        )

        action_url = (
            f"{self.get_ipms_base_url()}"
            "/notifications"
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
                        "Manager Status",
                        "Finance Status",
                    ],
                    "table_values": [
                        purchase_order.po_number,
                        "Direct PO",
                        purchase_order.vendor_name
                        or "-",
                        total_quantity,
                        components_display,
                        "Pending Manager Approval",
                        "Waiting for Manager Approval",
                    ],
                    "status":
                        "Pending Manager Approval",
                    "instruction": (
                        "Please open Notifications -> PO "
                        "and approve or reject this Direct PO. "
                        "Finance approval will start only after "
                        "Manager approval."
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
                    Prefetch(
                        "items",
                        queryset=(
                            PurchaseOrderItem.objects
                            .select_related("component")
                        ),
                    ),
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
                    Prefetch(
                        "items",
                        queryset=(
                            PurchaseOrderItem.objects
                            .select_related("component")
                        ),
                    ),
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
                    Prefetch(
                        "items",
                        queryset=(
                            PurchaseOrderItem.objects
                            .select_related("component")
                        ),
                    ),
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
    # PURCHASE ORDER PDF / VENDOR VOUCHER NUMBER
    # ==========================================================

    @staticmethod
    def _pdf_money_decimal(value):
        try:
            return Decimal(
                str(
                    value
                    if value not in (None, "")
                    else "0"
                )
            )
        except (
            TypeError,
            ValueError,
            ArithmeticError,
        ):
            return Decimal("0.00")

    @staticmethod
    def _pdf_vendor_code(vendor_name):
        """
        AERO / Aero360 -> AER

        Only alphabetic characters are used. Very short names are padded with
        X so the generated format always has a stable 3-character prefix.
        """
        letters = re.sub(
            r"[^A-Za-z]",
            "",
            str(vendor_name or ""),
        ).upper()

        if not letters:
            return "VEN"

        return letters[:3].ljust(3, "X")

    @staticmethod
    def _pdf_financial_year_code(date_value=None):
        """
        Indian financial year (April -> March), using underscore format.

        Example:
            10-Sep-2026 -> 26_27
            10-Feb-2027 -> 26_27
        """
        current_date = date_value or timezone.localdate()
        start_year = (
            current_date.year
            if current_date.month >= 4
            else current_date.year - 1
        )
        end_year = start_year + 1

        return (
            f"{start_year % 100:02d}_"
            f"{end_year % 100:02d}"
        )

    def _create_pdf_voucher_record(
        self,
        *,
        vendor_name,
        reference_po_numbers,
        selected_item_ids,
    ):
        """
        Atomically allocate the next vendor/FY PDF voucher number.

        A unique DB constraint protects against two users generating the same
        sequence at the same time. In the rare race where both requests see
        the same previous sequence, the losing request retries.
        """
        clean_vendor_name = str(
            vendor_name or "Vendor"
        ).strip() or "Vendor"

        vendor_code = self._pdf_vendor_code(
            clean_vendor_name
        )
        financial_year = (
            self._pdf_financial_year_code()
        )

        references = []
        for value in reference_po_numbers or []:
            value = str(value or "").strip()
            if value and value not in references:
                references.append(value)

        item_ids = []
        for value in selected_item_ids or []:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value not in item_ids:
                item_ids.append(value)

        generated_by = str(
            self.get_authenticated_po_sender_name()
            or ""
        ).strip()[:150]

        for attempt in range(5):
            try:
                with transaction.atomic():
                    latest = (
                        PurchaseOrderPdfVoucher.objects
                        .select_for_update()
                        .filter(
                            vendor_code=vendor_code,
                            financial_year=financial_year,
                        )
                        .order_by(
                            "-sequence",
                            "-id",
                        )
                        .first()
                    )

                    next_sequence = (
                        int(latest.sequence)
                        if latest
                        else 0
                    ) + 1

                    voucher_number = (
                        f"{vendor_code}/"
                        f"{financial_year}/"
                        f"{next_sequence:04d}"
                    )

                    return (
                        PurchaseOrderPdfVoucher.objects
                        .create(
                            vendor_name=clean_vendor_name,
                            vendor_code=vendor_code,
                            financial_year=financial_year,
                            sequence=next_sequence,
                            voucher_number=voucher_number,
                            reference_po_numbers=references,
                            selected_item_ids=item_ids,
                            generated_by=generated_by,
                        )
                    )

            except IntegrityError:
                if attempt == 4:
                    raise

        raise RuntimeError(
            "Unable to allocate Purchase Order PDF voucher number."
        )

    @staticmethod
    def _pdf_vendor_context(vendor, vendor_name, fallback_po=None):
        return {
            "name": (
                vendor.name
                if vendor
                else vendor_name
            ),
            "address": (
                getattr(vendor, "address", "")
                if vendor
                else ""
            ),
            "city": (
                getattr(vendor, "city", "")
                if vendor
                else ""
            ),
            "pincode": (
                getattr(vendor, "pincode", "")
                if vendor
                else ""
            ),
            "gstin": (
                getattr(vendor, "gst_number", "")
                if vendor
                else getattr(fallback_po, "gstin", "")
            ),
            "state": (
                getattr(vendor, "state", "")
                if vendor
                else ""
            ),
            "state_code": (
                getattr(vendor, "state_code", "")
                if vendor
                else ""
            ),
            "terms_and_conditions": (
                getattr(
                    vendor,
                    "terms_and_conditions",
                    "",
                )
                if vendor
                else ""
            ) or "",
        }

    def _build_purchase_order_pdf_response(
        self,
        *,
        selected_items,
        reference_purchase_orders,
        vendor_name,
        round_off,
        is_partial_selection,
    ):
        """
        Render one PDF from one or many same-vendor Purchase Orders.

        The existing PurchaseOrder.po_number is never changed. A separate
        vendor voucher number is allocated only for the generated PDF.
        """
        if not selected_items:
            return Response(
                {"detail": "No Purchase Order line items found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not reference_purchase_orders:
            return Response(
                {"detail": "No source Purchase Order found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        representative_po = reference_purchase_orders[0]
        clean_vendor_name = str(
            vendor_name
            or representative_po.vendor_name
            or ""
        ).strip()

        vendor = (
            Vendor.objects
            .filter(name__iexact=clean_vendor_name)
            .first()
        )

        reference_numbers = []
        for po in reference_purchase_orders:
            po_number = str(
                getattr(po, "po_number", "")
                or ""
            ).strip()
            if po_number and po_number not in reference_numbers:
                reference_numbers.append(po_number)

        voucher_record = self._create_pdf_voucher_record(
            vendor_name=clean_vendor_name,
            reference_po_numbers=reference_numbers,
            selected_item_ids=[
                item.id
                for item in selected_items
            ],
        )

        generated_date = timezone.localdate()
        money_decimal = self._pdf_money_decimal

        pdf_items = []
        subtotal = Decimal("0.00")
        discount_total = Decimal("0.00")
        taxable_total = Decimal("0.00")
        gst_total = Decimal("0.00")
        gst_rates = set()
        freight_total = Decimal("0.00")
        freight_gst_total = Decimal("0.00")
        items_total = Decimal("0.00")
        total_quantity = 0

        for index, item in enumerate(
            selected_items,
            start=1,
        ):
            component = getattr(
                item,
                "component",
                None,
            )
            source_po = getattr(
                item,
                "purchase_order",
                None,
            )

            quantity = max(
                int(
                    getattr(item, "quantity", 0)
                    or 0
                ),
                0,
            )

            unit_price = money_decimal(
                getattr(item, "unit_price", 0)
            )
            discount = max(
                money_decimal(
                    getattr(item, "discount", 0)
                ),
                Decimal("0.00"),
            )
            gst_percentage = max(
                money_decimal(
                    getattr(item, "gst_percentage", 0)
                ),
                Decimal("0.00"),
            )

            if gst_percentage > Decimal("0.00"):
                gst_rates.add(gst_percentage)

            freight_cost = max(
                money_decimal(
                    getattr(item, "freight_cost", 0)
                ),
                Decimal("0.00"),
            )
            freight_gst_percentage = max(
                money_decimal(
                    getattr(
                        item,
                        "freight_gst_percentage",
                        0,
                    )
                ),
                Decimal("0.00"),
            )

            line_subtotal = (
                Decimal(quantity) * unit_price
            )
            line_taxable_amount = max(
                line_subtotal - discount,
                Decimal("0.00"),
            )
            gst_amount = (
                line_taxable_amount
                * gst_percentage
                / Decimal("100")
            )
            freight_gst_amount = (
                freight_cost
                * freight_gst_percentage
                / Decimal("100")
            )
            line_total = (
                line_taxable_amount
                + gst_amount
                + freight_cost
                + freight_gst_amount
            )

            subtotal += line_subtotal
            discount_total += discount
            taxable_total += line_taxable_amount
            gst_total += gst_amount
            freight_total += freight_cost
            freight_gst_total += freight_gst_amount
            items_total += line_total
            total_quantity += quantity

            item_uom = str(
                getattr(item, "uom", "")
                or ""
            ).strip()
            item_hsn = str(
                getattr(item, "hsn_no", "")
                or ""
            ).strip()
            component_hsn = str(
                getattr(
                    component,
                    "hsn_numbers",
                    "",
                )
                or ""
            ).strip()

            pdf_items.append(
                {
                    "sl_no": index,
                    "name": (
                        getattr(component, "name", "")
                        if component
                        else "Component"
                    ) or "Component",
                    "component_id": (
                        getattr(
                            component,
                            "component_id",
                            "",
                        )
                        if component
                        else ""
                    ),
                    "part_number": (
                        getattr(
                            component,
                            "part_numbers",
                            "",
                        )
                        if component
                        else ""
                    ),
                    "specification": (
                        getattr(
                            component,
                            "specifications",
                            "",
                        )
                        if component
                        else ""
                    ),
                    "hsn": item_hsn or component_hsn,
                    "uom": item_uom or "Nos",
                    "due_date": (
                        getattr(
                            item,
                            "expected_delivery_date",
                            None,
                        )
                        or getattr(
                            source_po,
                            "expected_delivery_date",
                            None,
                        )
                    ),
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "subtotal": line_subtotal,
                    "discount": discount,
                    "taxable_amount": line_taxable_amount,
                    "gst_percentage": gst_percentage,
                    "gst_amount": gst_amount,
                    "freight_cost": freight_cost,
                    "freight_gst_percentage": (
                        freight_gst_percentage
                    ),
                    "freight_gst_amount": (
                        freight_gst_amount
                    ),
                    "line_total": line_total,
                    "total_cost": line_total,
                    "amount": line_total,

                    # IMPORTANT: every consolidated-PDF row keeps the
                    # original Purchase Order it came from.
                    "source_po_number": str(
                        getattr(
                            source_po,
                            "po_number",
                            "",
                        )
                        or ""
                    ).strip(),
                }
            )

        round_off = money_decimal(round_off)
        grand_total = items_total + round_off

        rounded_grand_total = grand_total.quantize(
            Decimal("0.01")
        )
        rupees = int(rounded_grand_total)
        paise = int(
            (
                rounded_grand_total
                - Decimal(rupees)
            )
            * 100
        )

        amount_in_words = (
            "INR "
            + num2words(
                rupees,
                lang="en_IN",
            ).title()
        )

        if paise:
            amount_in_words += (
                " And "
                + num2words(
                    paise,
                    lang="en_IN",
                ).title()
                + " Paise"
            )

        amount_in_words += " Only"

        company_context = {
            "name": "Dronix Technologies Private Limited",
            "address_line_1": "No.133, AC Complex, Ground Floor",
            "address_line_2": "Gandhi Road, Alapakkam, Perungalathur",
            "city": "Chennai",
            "gstin": "33AAGCD1081K1ZS",
            "state": "Tamil Nadu",
            "state_code": "33",
            "email": "finance@aero360.co.in",
        }

        vendor_context = self._pdf_vendor_context(
            vendor,
            clean_vendor_name,
            representative_po,
        )

        # ------------------------------------------------------
        # GST STATE VALIDATION
        # ------------------------------------------------------
        # Dronix is in Tamil Nadu (State Code 33).
        # Supplier in Tamil Nadu -> CGST + SGST.
        # Supplier in another state -> IGST.
        company_state_code = str(
            company_context.get("state_code") or ""
        ).strip()

        vendor_state_code = str(
            vendor_context.get("state_code") or ""
        ).strip()

        company_state_name = " ".join(
            str(
                company_context.get("state") or ""
            ).strip().lower().split()
        )

        vendor_state_name = " ".join(
            str(
                vendor_context.get("state") or ""
            ).strip().lower().split()
        )

        usable_vendor_state_code = (
            vendor_state_code
            if vendor_state_code.upper()
            not in {"", "-", "NONE", "NULL"}
            else ""
        )

        if usable_vendor_state_code:
            is_intra_state = (
                usable_vendor_state_code
                == company_state_code
            )
        else:
            is_intra_state = bool(
                vendor_state_name
                and vendor_state_name
                == company_state_name
            )

        # Show a percentage only when all selected lines use one GST rate.
        # Example: 18% -> CGST 9% + SGST 9%, or IGST 18%.
        common_gst_rate = (
            next(iter(gst_rates))
            if len(gst_rates) == 1
            else None
        )

        if is_intra_state:
            cgst_total = (
                gst_total / Decimal("2")
            ).quantize(Decimal("0.01"))

            # Keep exact tax total after rounding.
            sgst_total = (
                gst_total - cgst_total
            ).quantize(Decimal("0.01"))

            igst_total = Decimal("0.00")

            if common_gst_rate is not None:
                cgst_rate_display = (
                    common_gst_rate / Decimal("2")
                )
                sgst_rate_display = (
                    common_gst_rate / Decimal("2")
                )
            else:
                cgst_rate_display = None
                sgst_rate_display = None

            igst_rate_display = None
            gst_tax_type = "CGST_SGST"
        else:
            cgst_total = Decimal("0.00")
            sgst_total = Decimal("0.00")
            igst_total = gst_total.quantize(
                Decimal("0.01")
            )

            cgst_rate_display = None
            sgst_rate_display = None
            igst_rate_display = common_gst_rate
            gst_tax_type = "IGST"

        # Retained only for backward compatibility.
        # The PDF template intentionally leaves Other References blank.
        other_reference = (
            f"CFRE / DRONIX "
            f"{voucher_record.voucher_number} / R0"
        )

        context = {
            "company": company_context,
            "vendor": vendor_context,
            "purchase_order": representative_po,

            # Existing PO ID remains available for backward compatibility.
            "po_number": representative_po.po_number,

            # New PDF-specific values.
            "voucher_number": voucher_record.voucher_number,
            "reference_numbers": reference_numbers,
            "reference_number": " / ".join(reference_numbers),
            "pdf_generated_date": generated_date,

            # Date box now means PDF generation date, per requirement.
            "po_date": generated_date,
            "other_reference": other_reference,
            "items": pdf_items,
            "subtotal": subtotal,
            "discount_total": discount_total,
            "taxable_total": taxable_total,
            "gst_total": gst_total,

            # State-based GST rendering values.
            "is_intra_state": is_intra_state,
            "gst_tax_type": gst_tax_type,
            "cgst_total": cgst_total,
            "sgst_total": sgst_total,
            "igst_total": igst_total,
            "cgst_rate_display": cgst_rate_display,
            "sgst_rate_display": sgst_rate_display,
            "igst_rate_display": igst_rate_display,

            "gst_label": (
                "CGST + SGST"
                if is_intra_state
                else "IGST"
            ),
            "freight_total": freight_total,
            "freight_gst_total": freight_gst_total,
            "items_total": items_total,
            "round_off": round_off,
            "po_round_off": round_off,
            "grand_total": grand_total,
            "total_quantity": total_quantity,
            "amount_in_words": amount_in_words,
            "is_partial_selection": is_partial_selection,
            "is_consolidated_pdf": (
                len(reference_numbers) > 1
            ),
        }

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
                    "detail": (
                        "Unable to generate Purchase Order PDF."
                    )
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        safe_voucher_number = (
            str(voucher_record.voucher_number)
            .replace("/", "-")
            .replace("\\", "-")
        )

        response = HttpResponse(
            result.getvalue(),
            content_type="application/pdf",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="PO_{safe_voucher_number}.pdf"'
        )
        response["X-PO-Voucher-Number"] = (
            voucher_record.voucher_number
        )
        response["Access-Control-Expose-Headers"] = (
            "Content-Disposition, X-PO-Voucher-Number"
        )

        return response

    @action(
        detail=True,
        methods=["get"],
        url_path="pdf",
    )
    def download_pdf(self, request, pk=None):
        """
        Generate a PDF from one Purchase Order.

        Voucher example:
            AER/26_27/0001

        Reference No. & Date contains the existing PO number and the PDF
        generation date. The existing PurchaseOrder.po_number is unchanged.
        """
        try:
            purchase_order = (
                PurchaseOrder.objects
                .prefetch_related(
                    Prefetch(
                        "items",
                        queryset=(
                            PurchaseOrderItem.objects
                            .select_related(
                                "component",
                                "purchase_order",
                            )
                        ),
                    ),
                )
                .get(pk=pk)
            )
        except PurchaseOrder.DoesNotExist:
            return Response(
                {"detail": "Purchase Order not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        all_items = list(
            purchase_order.items.all()
        )
        all_item_ids = [
            int(item.id)
            for item in all_items
        ]

        requested_item_ids_raw = str(
            request.query_params.get(
                "item_ids",
                "",
            )
            or ""
        ).strip()

        requested_item_ids = []

        if requested_item_ids_raw:
            for value in requested_item_ids_raw.split(","):
                value = str(value or "").strip()

                if not value:
                    continue

                if not value.isdigit():
                    return Response(
                        {
                            "detail": (
                                "Invalid Purchase Order line-item "
                                "selection."
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                item_id = int(value)
                if item_id not in requested_item_ids:
                    requested_item_ids.append(item_id)

            if not requested_item_ids:
                return Response(
                    {
                        "detail": (
                            "Select at least one Purchase Order "
                            "line item."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            all_item_id_set = set(all_item_ids)
            if not set(requested_item_ids).issubset(
                all_item_id_set
            ):
                return Response(
                    {
                        "detail": (
                            "One or more selected line items do not "
                            "belong to this Purchase Order."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            requested_item_id_set = set(
                requested_item_ids
            )
            selected_items = [
                item
                for item in all_items
                if int(item.id) in requested_item_id_set
            ]
        else:
            selected_items = all_items

        if not selected_items:
            return Response(
                {"detail": "No Purchase Order line items found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        selected_id_set = {
            int(item.id)
            for item in selected_items
        }
        is_partial_selection = (
            selected_id_set != set(all_item_ids)
        )

        round_off = (
            Decimal("0.00")
            if is_partial_selection
            else self._pdf_money_decimal(
                purchase_order.round_off
            )
        )

        return self._build_purchase_order_pdf_response(
            selected_items=selected_items,
            reference_purchase_orders=[purchase_order],
            vendor_name=purchase_order.vendor_name,
            round_off=round_off,
            is_partial_selection=is_partial_selection,
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="vendor-pdf",
    )
    def vendor_pdf(self, request):
        """
        Generate ONE consolidated PDF for selected components from multiple
        Purchase Orders belonging to the SAME vendor.

        Expected body:
            {
                "vendor_name": "Aero360",
                "selections": [
                    {"po_id": 34, "item_ids": [101, 102]},
                    {"po_id": 36, "item_ids": [110]}
                ]
            }

        The PDF receives one new vendor voucher number such as
        AER/26_27/0001. Every selected BOM row keeps its own source PO number.
        """
        vendor_name = str(
            request.data.get("vendor_name")
            or ""
        ).strip()
        selections = request.data.get(
            "selections",
            [],
        )

        if not isinstance(selections, list):
            return Response(
                {
                    "detail": (
                        "selections must be a list of PO/item selections."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Merge duplicate entries for the same PO while preserving order.
        selection_map = {}

        for entry in selections:
            if not isinstance(entry, dict):
                continue

            raw_po_id = entry.get("po_id")
            raw_item_ids = entry.get("item_ids", [])

            try:
                po_id = int(raw_po_id)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "Invalid Purchase Order ID."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if not isinstance(raw_item_ids, list):
                return Response(
                    {
                        "detail": (
                            "item_ids must be a list for each "
                            "Purchase Order."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            selected_ids = selection_map.setdefault(
                po_id,
                [],
            )

            for raw_item_id in raw_item_ids:
                try:
                    item_id = int(raw_item_id)
                except (TypeError, ValueError):
                    return Response(
                        {
                            "detail": (
                                "Invalid Purchase Order line-item ID."
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if item_id not in selected_ids:
                    selected_ids.append(item_id)

        selection_map = {
            po_id: item_ids
            for po_id, item_ids in selection_map.items()
            if item_ids
        }

        if not selection_map:
            return Response(
                {
                    "detail": (
                        "Select at least one component before "
                        "generating the PDF."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        po_ids = list(selection_map.keys())
        purchase_orders = list(
            PurchaseOrder.objects
            .filter(pk__in=po_ids)
            .prefetch_related(
                Prefetch(
                    "items",
                    queryset=(
                        PurchaseOrderItem.objects
                        .select_related(
                            "component",
                            "purchase_order",
                        )
                    ),
                ),
            )
        )
        po_by_id = {
            int(po.id): po
            for po in purchase_orders
        }

        if set(po_by_id.keys()) != set(po_ids):
            return Response(
                {
                    "detail": (
                        "One or more selected Purchase Orders "
                        "could not be found."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # If the client did not send a vendor name, safely derive it from the
        # first source PO. All source POs are then validated against it.
        if not vendor_name:
            first_po = po_by_id[po_ids[0]]
            vendor_name = str(
                first_po.vendor_name or ""
            ).strip()

        vendor_key = vendor_name.casefold()

        selected_items = []
        reference_purchase_orders = []
        total_round_off = Decimal("0.00")
        is_partial_selection = False
        seen_item_ids = set()

        for po_id in po_ids:
            purchase_order = po_by_id[po_id]

            if str(
                purchase_order.vendor_name or ""
            ).strip().casefold() != vendor_key:
                return Response(
                    {
                        "detail": (
                            "All Purchase Orders in one generated PDF "
                            "must belong to the same vendor."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            all_items = list(
                purchase_order.items.all()
            )
            all_item_by_id = {
                int(item.id): item
                for item in all_items
            }

            requested_ids = selection_map[po_id]
            requested_id_set = set(requested_ids)

            if not requested_id_set.issubset(
                set(all_item_by_id.keys())
            ):
                return Response(
                    {
                        "detail": (
                            f"One or more selected items do not "
                            f"belong to PO {purchase_order.po_number}."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            selected_for_po = [
                all_item_by_id[item_id]
                for item_id in requested_ids
                if item_id in all_item_by_id
            ]

            if not selected_for_po:
                continue

            reference_purchase_orders.append(
                purchase_order
            )

            all_ids = set(all_item_by_id.keys())
            if requested_id_set == all_ids:
                total_round_off += (
                    self._pdf_money_decimal(
                        purchase_order.round_off
                    )
                )
            else:
                # A PO-level round-off is not applied to a partial selection.
                is_partial_selection = True

            for item in selected_for_po:
                if int(item.id) in seen_item_ids:
                    continue
                seen_item_ids.add(int(item.id))
                selected_items.append(item)

        if not selected_items:
            return Response(
                {
                    "detail": (
                        "No valid Purchase Order components were selected."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return self._build_purchase_order_pdf_response(
            selected_items=selected_items,
            reference_purchase_orders=(
                reference_purchase_orders
            ),
            vendor_name=vendor_name,
            round_off=total_round_off,
            is_partial_selection=is_partial_selection,
        )

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
        Return the correct component rows for one Material Request.

        BOM
            -> bom_items

        R&D
            -> rd_items

        RETURNABLE / RETAIL_SALES
            -> request_items

        PR / FR children keep the request_type copied from their
        source MR, so a Returnable _PR/_FR must continue using
        request_items.
        """

        request_type = str(
            material_request.request_type or ""
        ).strip().upper()

        if request_type in {"R&D", "RD"}:
            manager = material_request.rd_items

        elif request_type in {
            "RETURNABLE",
            "RETAIL_SALES",
        }:
            manager = getattr(
                material_request,
                "request_items",
                None,
            )

            if manager is None:
                raise ValidationError({
                    "items": [
                        (
                            "This Material Request type "
                            "requires request_items."
                        )
                    ]
                })

        else:
            manager = material_request.bom_items

        queryset = (
            manager
            .all()
            .order_by("id")
        )

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

    @action(
        detail=False,
        methods=["get"],
        url_path="mr-shortage-summary",
    )
    def mr_shortage_summary(
        self,
        request,
    ):
        """
        Return the authoritative Procurement shortage for one Material
        Request, including quantities already covered by existing active
        STANDARD Purchase Orders.

        This endpoint intentionally uses the SAME rules as PO creation
        validation so the Procurement UI cannot show stale "PO Remaining"
        values that later fail during POST.
        """
        source_mr_number = str(
            request.query_params.get("mr")
            or request.query_params.get(
                "source_mr_number"
            )
            or ""
        ).strip()

        if not source_mr_number:
            return Response(
                {
                    "detail":
                        "Material Request number is required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        material_request = (
            self.get_source_material_request(
                source_mr_number
            )
        )

        if not material_request:
            return Response(
                {
                    "detail":
                        "Material Request not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        request_items = (
            self.get_material_request_items(
                material_request
            )
        )

        shortage_groups = (
            self.get_reservation_shortages(
                material_request,
                request_items,
            )
        )

        active_standard_pos = (
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
                purchase_order__in=
                    active_standard_pos
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

        components = []

        for (
            component_id,
            group,
        ) in shortage_groups.items():
            component = (
                Component.objects
                .filter(pk=component_id)
                .first()
            )

            requested_quantity = int(
                group.get(
                    "required_quantity",
                    0,
                )
                or 0
            )

            reserved_store_quantity = int(
                group.get(
                    "reserved_store_quantity",
                    0,
                )
                or 0
            )

            procurement_shortage_quantity = int(
                group.get(
                    "shortage_quantity",
                    0,
                )
                or 0
            )

            already_ordered_quantity = int(
                ordered_by_component.get(
                    component_id,
                    0,
                )
                or 0
            )

            remaining_quantity = max(
                procurement_shortage_quantity
                - already_ordered_quantity,
                0,
            )

            first_item = (
                group.get("items") or [None]
            )[0]

            category = str(
                getattr(
                    first_item,
                    "category",
                    "",
                )
                or getattr(
                    component,
                    "category",
                    "",
                )
                or ""
            )

            components.append(
                {
                    "component_id":
                        component_id,
                    "component_code": str(
                        getattr(
                            component,
                            "component_id",
                            "",
                        )
                        or component_id
                    ),
                    "component_name": str(
                        getattr(
                            component,
                            "name",
                            "",
                        )
                        or "Component"
                    ),
                    "category": category,
                    "requested_quantity":
                        requested_quantity,
                    "reserved_store_quantity":
                        reserved_store_quantity,
                    "procurement_shortage_quantity":
                        procurement_shortage_quantity,
                    "already_ordered_quantity":
                        already_ordered_quantity,
                    "remaining_quantity":
                        remaining_quantity,
                }
            )

        return Response(
            {
                "material_request_id":
                    material_request
                    .material_request_id,
                "components": components,
            }
        )


    def validate_po_against_reserved_shortage(
        self,
        material_request,
        purchase_order,
    ):
        """
        Validate ONLY the components being created in the current PO.

        Why this is important:
        The old implementation re-validated every historical active PO
        attached to the MR. If an older PO row was already inconsistent,
        a completely valid new combined PO could be rejected even when
        its own component quantities were within the remaining shortage.

        For every component in the CURRENT PO:

            existing active ordered quantity
          + current PO quantity
          <= reserved Procurement shortage

        This works for:
        - one component in one PO,
        - multiple components in one combined PO,
        - separate POs,
        - partial quantities raised over multiple POs.
        """
        request_items = self.get_material_request_items(
            material_request,
            lock=True,
        )

        shortage_groups = self.get_reservation_shortages(
            material_request,
            request_items,
            lock=True,
        )

        # ------------------------------------------------------
        # Quantity being created in THIS PO, grouped by component.
        # Multiple rows for the same component are safely summed.
        # ------------------------------------------------------
        current_rows = (
            PurchaseOrderItem.objects
            .filter(
                purchase_order=purchase_order
            )
            .values("component_id")
            .annotate(
                ordered_quantity=Sum("quantity")
            )
        )

        current_by_component = {
            int(row["component_id"]): int(
                row["ordered_quantity"] or 0
            )
            for row in current_rows
            if row["component_id"] is not None
        }

        if not current_by_component:
            return

        # ------------------------------------------------------
        # Quantities already ordered BEFORE this new PO.
        #
        # Important: exclude the current PO itself; otherwise its quantity
        # would be counted twice when we add current_by_component below.
        # ------------------------------------------------------
        previous_active_pos = (
            PurchaseOrder.objects
            .filter(
                source_mr_number=(
                    material_request.material_request_id
                ),
                order_type="STANDARD",
            )
            .exclude(pk=purchase_order.pk)
            .exclude(
                status__in=[
                    "REJECTED",
                    "FINANCE_REJECTED",
                ]
            )
        )

        previous_rows = (
            PurchaseOrderItem.objects
            .filter(
                purchase_order__in=
                    previous_active_pos,
                component_id__in=
                    list(
                        current_by_component.keys()
                    ),
            )
            .values("component_id")
            .annotate(
                ordered_quantity=Sum("quantity")
            )
        )

        previous_by_component = {
            int(row["component_id"]): int(
                row["ordered_quantity"] or 0
            )
            for row in previous_rows
            if row["component_id"] is not None
        }

        errors = []

        for (
            component_id,
            current_quantity,
        ) in current_by_component.items():
            allowed_shortage = int(
                shortage_groups
                .get(component_id, {})
                .get(
                    "shortage_quantity",
                    0,
                )
                or 0
            )

            previous_quantity = int(
                previous_by_component.get(
                    component_id,
                    0,
                )
                or 0
            )

            remaining_before_current = max(
                allowed_shortage
                - previous_quantity,
                0,
            )

            resulting_quantity = (
                previous_quantity
                + current_quantity
            )

            if (
                resulting_quantity
                > allowed_shortage
            ):
                component = (
                    Component.objects
                    .filter(pk=component_id)
                    .first()
                )

                component_code = str(
                    getattr(
                        component,
                        "component_id",
                        "",
                    )
                    or component_id
                )

                component_name = str(
                    getattr(
                        component,
                        "name",
                        "",
                    )
                    or "Component"
                )

                errors.append(
                    (
                        f"{component_code} - "
                        f"{component_name}: "
                        f"PO Qty {current_quantity}, "
                        f"Remaining Procurement shortage "
                        f"{remaining_before_current} "
                        f"(Reserved shortage "
                        f"{allowed_shortage}, "
                        f"already ordered "
                        f"{previous_quantity})."
                    )
                )

        if errors:
            raise ValidationError(
                {
                    "items": [
                        (
                            "PO quantity exceeds the "
                            "remaining reserved Procurement "
                            "shortage for one or more "
                            "selected components."
                        )
                    ],
                    # Keep these as strings so the frontend/API helper
                    # shows useful text instead of [object Object].
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
        Create a PO and start the correct approval workflow.

        Direct STANDARD PO:
            Create -> Manager -> Finance -> Ordered -> Delivery

        MR-linked STANDARD PO:
            Existing Finance workflow remains unchanged.

        QC Replacement PO:
            Existing Procurement replacement workflow remains unchanged.
        """
        purchase_order = serializer.save()
        invalidate_purchase_order_cache()

        if purchase_order.source_mr_number:
            material_request = (
                self.get_source_material_request(
                    purchase_order.source_mr_number,
                    lock=True,
                )
            )

            if material_request:
                self.validate_po_against_reserved_shortage(
                    material_request,
                    purchase_order,
                )

            self.sync_material_request_po_progress(
                purchase_order.source_mr_number
            )

            transaction.on_commit(
                lambda po_id=purchase_order.id: (
                    self.send_mr_requester_po_raised_email(
                        po_id
                    )
                )
            )

        # ------------------------------------------------------
        # DIRECT STANDARD PO -> MANAGER FIRST
        # ------------------------------------------------------
        if self.is_direct_standard_po(
            purchase_order
        ):
            update_fields = []

            if str(
                purchase_order.status
                or ""
            ).upper() != "PENDING":
                purchase_order.status = "PENDING"
                update_fields.append("status")

            if str(
                purchase_order.approval_status
                or ""
            ).upper() != "PENDING":
                purchase_order.approval_status = "PENDING"
                update_fields.append(
                    "approval_status"
                )

            if update_fields:
                purchase_order.save(
                    update_fields=update_fields
                )

            # Direct PO must NOT be visible to Finance before Manager approval.
            Notification.objects.filter(
                category="PO",
                receiver="FINANCE",
                reference_id=str(
                    purchase_order.id
                ),
            ).delete()

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

            return

        # ------------------------------------------------------
        # NON-DIRECT PO: preserve existing Finance behaviour.
        # ------------------------------------------------------
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
        invalidate_purchase_order_cache()

        new_status = str(
            purchase_order.status
            or ""
        ).upper()

        if (
            self.is_direct_standard_po(
                purchase_order
            )
            and new_status == "ORDERED"
            and old_status != "FINANCE_APPROVED"
        ):
            raise ValidationError(
                {
                    "status": (
                        "Finance approval is required "
                        "before a Direct PO can be "
                        "marked Ordered."
                    )
                }
            )

        new_approval_status = str(
            purchase_order.approval_status
            or ""
        ).upper()

        # A generic PATCH must never bypass the Manager-first Direct PO flow.
        # Only direct_manager_approve() is allowed to move a Direct PO from
        # PENDING to PENDING_FINANCE.
        if (
            self.is_direct_standard_po(
                purchase_order
            )
            and new_approval_status
            == "PENDING_FINANCE"
            and old_status
            != "PENDING_FINANCE"
        ):
            raise ValidationError(
                {
                    "approval_status": (
                        "Manager approval is required "
                        "before a Direct PO can be sent "
                        "to Finance."
                    )
                }
            )

        # Finance can act on a Direct PO only after Manager approval has
        # already moved it to PENDING_FINANCE.
        if (
            self.is_direct_standard_po(
                purchase_order
            )
            and new_approval_status in {
                "FINANCE_APPROVED",
                "FINANCE_REJECTED",
            }
            # Only validate when Finance approval_status is actually
            # CHANGING. A later ORDERED / delivery update keeps the
            # already-approved FINANCE_APPROVED value and must pass.
            and old_approval_status
            != new_approval_status
            and old_status
            != "PENDING_FINANCE"
        ):
            raise ValidationError(
                {
                    "approval_status": (
                        "This Direct PO is not pending "
                        "Finance approval."
                    )
                }
            )

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

            # Finance approval is the FINAL approval stage for a Direct PO.
            # After this, Procurement can Mark as Ordered.
            #
            # MR-linked standard PO keeps its existing Finance result email.
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

        invalidate_po_notification_cache()

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
        purchase_order_id = str(instance.id)

        instance.delete()

        Notification.objects.filter(
            category="PO",
            reference_id=purchase_order_id,
        ).delete()

        invalidate_po_notification_cache()
        invalidate_purchase_order_cache()

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
        FIRST approval stage for a Direct STANDARD PO.

        Direct PO:
            PENDING (Manager)
                -> PENDING_FINANCE
                -> FINANCE_APPROVED
                -> ORDERED
                -> Delivery

        MR-linked standard POs and QC Replacement POs are unchanged.
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
                    Prefetch(
                        "items",
                        queryset=(
                            PurchaseOrderItem.objects
                            .select_related("component")
                        ),
                    ),
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
            != "PENDING"
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

        manager_notification = (
            Notification.objects
            .filter(
                category="PO",
                receiver="MANAGER",
                reference_id=str(
                    purchase_order.id
                ),
            )
            .order_by("-created_at", "-id")
            .first()
        )

        original_sender = str(
            getattr(
                manager_notification,
                "requested_by",
                "",
            )
            or ""
        ).strip()[:150]

        actor = self.get_request_actor_name(
            request
        )

        purchase_order.status = (
            "PENDING_FINANCE"
        )
        purchase_order.approval_status = (
            "PENDING_FINANCE"
        )
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

        invalidate_po_notification_cache()

        # Finance notification is created ONLY AFTER Manager approval.
        self.save_finance_notification_with_sender(
            purchase_order,
            requested_by_override=(
                original_sender
            ),
        )

        transaction.on_commit(
            lambda po_id=purchase_order.id: (
                self.send_finance_approval_email(
                    po_id
                )
            )
        )

        invalidate_po_notification_cache()
        invalidate_purchase_order_cache()

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
        Manager rejects a Direct STANDARD PO before it reaches Finance.
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
                    Prefetch(
                        "items",
                        queryset=(
                            PurchaseOrderItem.objects
                            .select_related("component")
                        ),
                    ),
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
            != "PENDING"
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

        # Keep approval_status inside the existing model choices.
        # The authoritative rejected state is PurchaseOrder.status.
        purchase_order.approval_status = "PENDING"
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

        # Defensive cleanup: a Manager-rejected Direct PO must never
        # remain visible to Finance.
        Notification.objects.filter(
            category="PO",
            receiver="FINANCE",
            reference_id=str(
                purchase_order.id
            ),
        ).delete()

        invalidate_po_notification_cache()
        invalidate_purchase_order_cache()

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

        self.sync_returnable_restore_outward_status(
            purchase_order,
            "FINANCE_APPROVED",
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

        self.sync_returnable_restore_outward_status(
            purchase_order,
            "FINANCE_REJECTED",
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

        purchase_order, error_response = (
            self.get_replacement_po_or_error(pk)
        )
        if error_response:
            return error_response

        current_status = str(
            purchase_order.status or ""
        ).strip().upper()

        # Idempotent handling: if a previous click already changed the DB but
        # the browser still showed stale REPLACEMENT_APPROVED data, return the
        # current PO instead of failing with HTTP 400.
        if current_status in {
            "REPLACEMENT_ORDERED",
            "REPLACEMENT_PARTIALLY_RECEIVED",
            "REPLACEMENT_RECEIVED",
        }:
            transaction.on_commit(invalidate_purchase_order_cache)
            transaction.on_commit(invalidate_po_notification_cache)
            return Response(
                self.get_serializer(purchase_order).data,
                status=status.HTTP_200_OK,
            )

        if current_status != "REPLACEMENT_APPROVED":
            return Response(
                {
                    "detail": (
                        "The Replacement PO must be approved "
                        "before it can be ordered."
                    )
                },
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

        self.sync_returnable_restore_outward_status(
            purchase_order,
            "ORDERED",
        )
        self.sync_replacement_mr_status(purchase_order)

        transaction.on_commit(invalidate_purchase_order_cache)
        transaction.on_commit(invalidate_po_notification_cache)

        purchase_order.refresh_from_db()

        return Response(
            self.get_serializer(purchase_order).data,
            status=status.HTTP_200_OK,
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
                    Prefetch(
                        "items",
                        queryset=(
                            PurchaseOrderItem.objects
                            .select_related("component")
                        ),
                    ),
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

        # IMPORTANT:
        # Purchase Order list responses are version-cached.
        # The receive endpoint changes PO status directly (ORDERED ->
        # PARTIALLY_DELIVERED / DELIVERED), so invalidate the list cache
        # immediately. Otherwise the PO table can continue showing
        # "Mark Delivery" even though the Inward record exists and the
        # database PO status is already DELIVERED.
        invalidate_purchase_order_cache()

        # Recalculate every component and every PO in
        # the linked Material Request.
        if purchase_order.source_mr_number:
            self.sync_material_request_po_progress(
                purchase_order.source_mr_number
            )

        purchase_order = (
            PurchaseOrder.objects
            .prefetch_related(
                Prefetch(
                    "items",
                    queryset=(
                        PurchaseOrderItem.objects
                        .select_related("component")
                    ),
                ),
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