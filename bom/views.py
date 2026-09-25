import threading
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from notifications.email_service import send_ipms_email
from notifications.models import Notification

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.response import Response
from django.core.cache import cache
from inventory_backend.cache_utils import (
    build_list_cache_key,
    get_cache_version,
    invalidate_cache_version,
)
from inventory_backend.pagination import (
    OptionalPageNumberPagination,
    apply_server_query_parameters,
)

from .models import BOM, BOMItem, MasterBOMPricing, ProjectBOM
from .serializers import (
    BOMItemSerializer,
    BOMSerializer,
    MasterBOMPricingSerializer,
    ProjectBOMSerializer,
)


User = get_user_model()

BOM_LIST_CACHE_TTL_SECONDS = 60
BOM_LIST_CACHE_VERSION_KEY = "ipms:bom:list:version"
BOM_ITEM_LIST_CACHE_TTL_SECONDS = 60
BOM_ITEM_LIST_CACHE_VERSION_KEY = "ipms:bom_items:list:version"

# BOM writes Notification rows directly, so it must also invalidate the
# shared Notification list cache used by Manager Notifications.
NOTIFICATION_LIST_CACHE_VERSION_KEY = "ipms:notifications:list:version"


def enqueue_bom_email(callback, *args, **kwargs):
    """Send workflow email after commit without blocking the API response."""
    threading.Thread(
        target=callback,
        args=args,
        kwargs=kwargs,
        daemon=True,
    ).start()


def get_bom_cache_version():
    return get_cache_version(BOM_LIST_CACHE_VERSION_KEY)


def invalidate_bom_cache():
    invalidate_cache_version(BOM_LIST_CACHE_VERSION_KEY)


def get_bom_item_cache_version():
    return get_cache_version(BOM_ITEM_LIST_CACHE_VERSION_KEY)


def invalidate_bom_item_cache():
    invalidate_cache_version(BOM_ITEM_LIST_CACHE_VERSION_KEY)


def invalidate_notification_list_cache():
    """Refresh notification lists after direct BOM workflow writes."""
    invalidate_cache_version(NOTIFICATION_LIST_CACHE_VERSION_KEY)


def get_actor_name(request, payload_field):
    user = getattr(request, "user", None)

    if (
        user
        and getattr(user, "is_authenticated", False)
    ):
        full_name = ""

        if hasattr(user, "get_full_name"):
            full_name = user.get_full_name()

        return (
            getattr(user, "employee_name", "")
            or full_name
            or getattr(user, "username", "")
            or getattr(user, "email", "")
            or "MANAGER"
        )

    return (
        request.data.get(payload_field)
        or request.data.get("manager_name")
        or "MANAGER"
    )



def get_ipms_base_url():
    return str(
        getattr(
            settings,
            "IPMS_BASE_URL",
            "http://localhost:5173",
        )
        or "http://localhost:5173"
    ).rstrip("/")


def send_manager_bom_review_email(
    bom_id,
    *,
    event_type,
    actor_name="",
):
    """
    Email every active Manager user when:
    - a new BOM is created, or
    - a rejected/edited BOM is modified and needs Manager review again.
    """
    try:
        bom = (
            BOM.objects
            .prefetch_related(
                Prefetch(
                    "items",
                    queryset=(
                        BOMItem.objects
                        .select_related("component")
                    ),
                ),
            )
            .get(pk=bom_id)
        )
    except BOM.DoesNotExist:
        return False

    managers = list(
        User.objects
        .filter(
            role__iexact="manager",
            is_active=True,
        )
        .exclude(email__isnull=True)
        .exclude(email="")
        .order_by("id")
    )

    if not managers:
        print(
            "BOM MANAGER EMAIL SKIPPED:",
            bom.bom_number,
            "- no active Manager user with email.",
        )
        return False

    normalized_event = str(
        event_type or ""
    ).strip().lower()

    actor = (
        str(actor_name or "").strip()
        or str(bom.created_by or "").strip()
        or "User"
    )

    if normalized_event == "modified":
        subject = (
            f"{bom.bom_number} modified by "
            f"{actor} - Approval Required"
        )

        message = (
            f"BOM {bom.bom_number} has been modified "
            f"by {actor} and requires Manager review again."
        )

        status_label = "Modified - Pending Manager Review"
    else:
        subject = (
            f"{bom.bom_number} created by "
            f"{actor} - Approval Required"
        )

        message = (
            f"A new BOM {bom.bom_number} was created "
            f"by {actor} and requires Manager approval."
        )

        status_label = "Pending Manager Approval"

    component_count = bom.items.count()

    component_lines = []

    for item in bom.items.all():
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
            or item.component_code
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
                f"(Qty: {int(item.quantity or 0)} {str(getattr(item, 'unit', '') or '').strip()})"
            ).strip()
        )

    component_summary = (
        "; ".join(component_lines)
        if component_lines
        else "-"
    )

    action_url = (
        f"{get_ipms_base_url()}"
        f"/notifications"
    )

    sent_any = False

    for manager in managers:
        sent = send_ipms_email(
            recipient_email=manager.email,
            subject=subject,
            context={
                "recipient_name": (
                    getattr(
                        manager,
                        "employee_name",
                        "",
                    )
                    or getattr(
                        manager,
                        "email",
                        "",
                    )
                    or "Manager"
                ),
                "message": message,
                "table_headers": [
                    "BOM Number",
                    "BOM Name",
                    "Product",
                    "Version",
                    "Created By",
                    "Action By",
                    "Components",
                    "Component Details",
                    "Status",
                ],
                "table_values": [
                    bom.bom_number,
                    bom.bom_name
                    or "-",
                    bom.product_name,
                    bom.version,
                    bom.created_by,
                    actor,
                    component_count,
                    component_summary,
                    status_label,
                ],
                "status": status_label,
                "instruction": (
                    "Please review this BOM from "
                    "Manager Notifications in IPMS."
                ),
                "button_text": (
                    "Review BOM in IPMS"
                ),
                "action_url": action_url,
            },
        )

        if sent:
            sent_any = True

    print(
        "BOM MANAGER EMAIL SENT =",
        sent_any,
        "| BOM =",
        bom.bom_number,
        "| EVENT =",
        normalized_event or "created",
    )

    return sent_any


def update_manager_notification(
    bom,
    notification_status,
    is_read,
    title=None,
    message=None,
    requested_by=None,
):
    """
    Update the existing BOM notification.

    Create it when an older BOM does not already have
    a Manager notification.
    """

    notification_data = {
        "status": notification_status,
        "is_read": is_read,
    }

    if requested_by:
        notification_data["requested_by"] = (
            str(requested_by).strip()[:150]
        )

    if title is not None:
        notification_data["title"] = title

    if message is not None:
        notification_data["message"] = message

    notification_queryset = (
        Notification.objects.filter(
            category="BOM",
            reference_id=str(bom.id),
            receiver="MANAGER",
        )
    )

    updated_count = notification_queryset.update(
        **notification_data
    )

    if updated_count == 0:
        Notification.objects.create(
            category="BOM",
            title=(
                title
                or f"BOM Approval Request - {bom.bom_number}"
            ),
            message=(
                message
                or (
                    f"BOM {bom.bom_number} requires "
                    f"manager approval."
                )
            ),
            reference_id=str(bom.id),
            status=notification_status,
            receiver="MANAGER",
            is_read=is_read,
            requested_by=(
                str(requested_by).strip()[:150]
                if requested_by
                else None
            ),
        )


    # These Notification writes bypass NotificationViewSet, whose list endpoint
    # is cached. Expire that shared cache only after the DB transaction commits.
    transaction.on_commit(invalidate_notification_list_cache)


def mark_bom_as_modified(
    bom,
    *,
    actor_name="",
):
    """
    Return a rejected BOM to Manager review after modification.

    IMPORTANT FIX:
    Do not rely only on bom.status to decide whether to email Manager.

    BOMSerializer.update() may already change:
        MANAGER_REJECTED -> MODIFIED
    before this function runs.

    In that case the old implementation saw MODIFIED and refreshed the
    Manager notification, but skipped the email.

    The Manager notification is the reliable workflow gate:
        MANAGER_REJECTED -> MODIFIED
    sends exactly one Manager re-review email.
    Later edits while notification is already MODIFIED do not resend.
    """

    current_status = str(
        bom.status or ""
    ).strip().upper()

    notification = (
        Notification.objects
        .filter(
            category="BOM",
            reference_id=str(bom.id),
            receiver="MANAGER",
        )
        .order_by("-id")
        .first()
    )

    previous_notification_status = str(
        getattr(
            notification,
            "status",
            "",
        )
        or ""
    ).strip().upper()

    # A Manager re-review email is required exactly when the previous
    # Manager decision was rejection.
    should_send_manager_email = (
        previous_notification_status
        == "MANAGER_REJECTED"
    )

    # Backward compatibility for older BOMs whose notification may be
    # missing or stale.
    if not previous_notification_status:
        should_send_manager_email = (
            current_status
            == "MANAGER_REJECTED"
        )

    if current_status not in {
        "MANAGER_REJECTED",
        "MODIFIED",
    }:
        return

    if current_status == "MANAGER_REJECTED":
        bom.status = "MODIFIED"

        bom.save(
            update_fields=[
                "status",
                "updated_at",
            ]
        )

    update_manager_notification(
        bom=bom,
        notification_status="MODIFIED",
        is_read=False,
        title=(
            f"Modified BOM Approval - "
            f"{bom.bom_number}"
        ),
        message=(
            f"BOM {bom.bom_number} was modified "
            f"after manager rejection and requires "
            f"manager approval again."
        ),
    )

    transaction.on_commit(invalidate_bom_cache)

    if should_send_manager_email:
        print(
            "BOM MODIFIED MANAGER EMAIL QUEUED:",
            bom.bom_number,
            "| previous notification status =",
            previous_notification_status
            or "(missing)",
        )

        transaction.on_commit(
            lambda bom_id=bom.id,
            actor=actor_name: (
                enqueue_bom_email(
                    send_manager_bom_review_email,
                    bom_id,
                    event_type="modified",
                    actor_name=actor,
                )
            )
        )
    else:
        print(
            "BOM MODIFIED MANAGER EMAIL NOT REPEATED:",
            bom.bom_number,
            "| previous notification status =",
            previous_notification_status
            or "(missing)",
        )


def resolve_bom_creator_user(bom):
    """
    Resolve the original BOM creator safely.

    Preferred source:
    - Manager BOM notification.requested_by (stored as authenticated email)

    Backward-compatible fallbacks:
    - BOM.created_by exact email
    - employee_name
    - username

    We never pick a random user.
    """
    creator_reference = ""

    notification = (
        Notification.objects
        .filter(
            category="BOM",
            reference_id=str(bom.id),
            receiver="MANAGER",
        )
        .order_by("-id")
        .first()
    )

    if notification:
        creator_reference = str(
            getattr(
                notification,
                "requested_by",
                "",
            )
            or ""
        ).strip()

    if not creator_reference:
        creator_reference = str(
            bom.created_by or ""
        ).strip()

    if not creator_reference:
        return None

    # Exact email is the strongest identity.
    user = (
        User.objects
        .filter(
            email__iexact=creator_reference,
            is_active=True,
        )
        .first()
    )

    if user:
        return user

    # Custom user model in this project uses employee_name.
    try:
        user = (
            User.objects
            .filter(
                employee_name__iexact=creator_reference,
                is_active=True,
            )
            .first()
        )
        if user:
            return user
    except Exception:
        pass

    # Username fallback for older user records.
    try:
        user = (
            User.objects
            .filter(
                username__iexact=creator_reference,
                is_active=True,
            )
            .first()
        )
        if user:
            return user
    except Exception:
        pass

    return None


def send_bom_creator_result_email(
    bom_id,
    *,
    outcome,
    action_by="Manager",
    rejection_reason="",
):
    """
    Return Manager's BOM decision to the original BOM creator.
    """
    try:
        bom = (
            BOM.objects
            .prefetch_related(
                Prefetch(
                    "items",
                    queryset=(
                        BOMItem.objects
                        .select_related("component")
                    ),
                ),
            )
            .get(pk=bom_id)
        )
    except BOM.DoesNotExist:
        return False

    creator = resolve_bom_creator_user(bom)

    if not creator:
        print(
            "BOM RESULT EMAIL SKIPPED:",
            bom.bom_number,
            "- original BOM creator could not be resolved.",
        )
        return False

    recipient_email = str(
        getattr(
            creator,
            "email",
            "",
        )
        or ""
    ).strip()

    if not recipient_email:
        print(
            "BOM RESULT EMAIL SKIPPED:",
            bom.bom_number,
            "- creator has no email.",
        )
        return False

    creator_name = (
        getattr(
            creator,
            "employee_name",
            "",
        )
        or getattr(
            creator,
            "email",
            "",
        )
        or str(bom.created_by or "User")
    )

    normalized_outcome = str(
        outcome or ""
    ).strip().lower()

    if normalized_outcome == "rejected":
        status_label = "Manager Rejected"
        subject = (
            f"{bom.bom_number} - Manager Rejected"
        )
        message = (
            f"Manager has rejected BOM "
            f"{bom.bom_number}."
        )
        instruction = (
            "Please review the rejection reason, "
            "modify the BOM, and resubmit it for "
            "Manager approval."
        )
    else:
        status_label = "Manager Approved"
        subject = (
            f"{bom.bom_number} - Manager Approved"
        )
        message = (
            f"Manager has approved BOM "
            f"{bom.bom_number}."
        )
        instruction = (
            "The BOM is approved and can continue "
            "through the IPMS workflow."
        )

    component_count = bom.items.count()

    sent = send_ipms_email(
        recipient_email=recipient_email,
        subject=subject,
        context={
            "recipient_name": creator_name,
            "message": message,
            "table_headers": [
                "BOM Number",
                "BOM Name",
                "Product",
                "Version",
                "Created By",
                "Components",
                "Decision",
                "Decision By",
                "Rejection Reason",
            ],
            "table_values": [
                bom.bom_number,
                bom.bom_name or "-",
                bom.product_name,
                bom.version,
                bom.created_by,
                component_count,
                status_label,
                action_by or "Manager",
                (
                    rejection_reason
                    if normalized_outcome == "rejected"
                    else "-"
                ),
            ],
            "status": status_label,
            "instruction": instruction,
            "button_text": (
                "View BOM in IPMS"
            ),
            "action_url": (
                f"{get_ipms_base_url()}"
                f"/bom"
            ),
        },
    )

    print(
        "BOM RESULT EMAIL SENT =",
        sent,
        "| BOM =",
        bom.bom_number,
        "| TO =",
        recipient_email,
        "| OUTCOME =",
        status_label,
    )

    return sent



class BOMViewSet(viewsets.ModelViewSet):
    serializer_class = BOMSerializer
    pagination_class = OptionalPageNumberPagination

    @transaction.atomic
    def perform_create(self, serializer):
        """
        New BOM:
        existing serializer creates the BOM + Manager notification.
        This view adds the Manager email after the DB transaction commits.
        """
        bom = serializer.save()

        actor_name = get_actor_name(
            self.request,
            "created_by",
        )

        request_user = getattr(
            self.request,
            "user",
            None,
        )

        creator_email = (
            str(
                getattr(
                    request_user,
                    "email",
                    "",
                )
                or ""
            ).strip()
            if (
                request_user
                and getattr(
                    request_user,
                    "is_authenticated",
                    False,
                )
            )
            else ""
        )

        # Keep the existing in-app Manager notification authoritative.
        # Store exact creator email in requested_by for reliable return mail.
        update_manager_notification(
            bom=bom,
            notification_status="PENDING_MANAGER",
            is_read=False,
            title=(
                f"BOM Approval Request - "
                f"{bom.bom_number}"
            ),
            message=(
                f"BOM {bom.bom_number} was created "
                f"by {bom.created_by} and requires "
                f"manager approval."
            ),
            requested_by=creator_email or None,
        )

        transaction.on_commit(invalidate_bom_cache)

        transaction.on_commit(
            lambda bom_id=bom.id,
            actor=actor_name: (
                enqueue_bom_email(
                    send_manager_bom_review_email,
                    bom_id,
                    event_type="created",
                    actor_name=actor,
                )
            )
        )

    @transaction.atomic
    def perform_update(self, serializer):
        """
        Main BOM Save/Update.

        If a Manager-rejected BOM is modified, return it to Manager and
        send exactly one re-review email. The email trigger is based on
        the previous Manager notification state, so it still works even
        when BOMSerializer.update() has already changed the BOM status
        from MANAGER_REJECTED to MODIFIED.
        """
        old_status = str(
            serializer.instance.status or ""
        ).strip().upper()

        bom = serializer.save()

        transaction.on_commit(invalidate_bom_cache)

        actor_name = get_actor_name(
            self.request,
            "modified_by",
        )

        new_status = str(
            bom.status or ""
        ).strip().upper()

        if (
            old_status == "MANAGER_REJECTED"
            or new_status == "MODIFIED"
        ):
            mark_bom_as_modified(
                bom,
                actor_name=actor_name,
            )

    @transaction.atomic
    def perform_destroy(self, instance):
        """
        Permanently delete the BOM and synchronize all related cached lists.

        BOMItem rows are deleted automatically by the BOM -> BOMItem CASCADE.
        BOM workflow notifications use a generic reference_id, so delete those
        explicitly to avoid stale Manager notification records.
        """
        bom_id = instance.pk

        Notification.objects.filter(
            category="BOM",
            reference_id=str(bom_id),
        ).delete()

        instance.delete()

        transaction.on_commit(
            invalidate_bom_cache
        )
        transaction.on_commit(
            invalidate_bom_item_cache
        )
        transaction.on_commit(
            invalidate_notification_list_cache
        )

    def list(self, request, *args, **kwargs):
        version = get_bom_cache_version()
        cache_key = None

        if version:
            cache_key = build_list_cache_key(
                "ipms:bom:list",
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
                    timeout=BOM_LIST_CACHE_TTL_SECONDS,
                )
            except Exception:
                pass

        return response

    def get_queryset(self):
        queryset = BOM.objects.all().order_by(
            "-created_at",
            "-id",
        )

        is_list_action = getattr(self, "action", "") == "list"
        summary_value = str(
            self.request.query_params.get(
                "summary",
                "",
            )
            or ""
        ).strip().lower()
        summary_mode = summary_value in {
            "1",
            "true",
            "yes",
            "on",
        }

        if not (is_list_action and summary_mode):
            queryset = queryset.prefetch_related(
                Prefetch(
                    "items",
                    queryset=(
                        BOMItem.objects
                        .select_related("component")
                    ),
                ),
            )

        bom_number = (
            self.request.query_params.get(
                "bom_number"
            )
        )

        if bom_number:
            queryset = queryset.filter(
                bom_number=bom_number
            )

        return apply_server_query_parameters(
            queryset,
            self.request,
            search_fields=(
                "bom_number",
                "bom_name",
                "product_name",
                "version",
                "created_by",
                "description",
                "status",
            ),
            filter_fields={
                "bom_number": "bom_number__icontains",
                "status": "status__iexact",
                "created_by": "created_by__icontains",
                "is_active": "is_active",
            },
            boolean_fields=("is_active",),
            ordering_fields=(
                "bom_number",
                "bom_name",
                "product_name",
                "version",
                "status",
                "created_at",
                "updated_at",
            ),
            default_ordering=("-created_at", "-id"),
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="approve",
    )
    @transaction.atomic
    def approve_bom(self, request, pk=None):
        bom = self.get_object()

        current_status = str(
            bom.status or ""
        ).upper()

        if current_status not in {
            "PENDING_MANAGER",
            "MODIFIED",
        }:
            return Response(
                {
                    "detail": (
                        "Only Pending Manager or Modified "
                        "BOMs can be approved."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        approved_by = get_actor_name(
            request,
            "approved_by",
        )

        bom.status = "APPROVED"
        bom.manager_approved_by = approved_by
        bom.manager_approved_at = timezone.now()

        # Clear old rejection information.
        bom.manager_rejection_reason = None
        bom.manager_rejected_by = None
        bom.manager_rejected_at = None

        bom.save(
            update_fields=[
                "status",
                "manager_approved_by",
                "manager_approved_at",
                "manager_rejection_reason",
                "manager_rejected_by",
                "manager_rejected_at",
                "updated_at",
            ]
        )

        # This must be before return Response.
        update_manager_notification(
            bom=bom,
            notification_status="APPROVED",
            is_read=True,
            title=(
                f"BOM Approved - "
                f"{bom.bom_number}"
            ),
            message=(
                f"BOM {bom.bom_number} was approved "
                f"by {approved_by}."
            ),
        )

        transaction.on_commit(invalidate_bom_cache)

        transaction.on_commit(
            lambda bom_id=bom.id,
            manager_name=approved_by: (
                enqueue_bom_email(
                    send_bom_creator_result_email,
                    bom_id,
                    outcome="approved",
                    action_by=manager_name,
                )
            )
        )

        serializer = self.get_serializer(bom)

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="reject",
    )
    @transaction.atomic
    def reject_bom(self, request, pk=None):
        bom = self.get_object()

        current_status = str(
            bom.status or ""
        ).upper()

        if current_status not in {
            "PENDING_MANAGER",
            "MODIFIED",
        }:
            return Response(
                {
                    "detail": (
                        "Only Pending Manager or Modified "
                        "BOMs can be rejected."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        remarks = str(
            request.data.get("remarks", "")
        ).strip()

        if not remarks:
            return Response(
                {
                    "remarks": [
                        "Rejection remarks are required."
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        rejected_by = get_actor_name(
            request,
            "rejected_by",
        )

        bom.status = "MANAGER_REJECTED"
        bom.manager_rejection_reason = remarks
        bom.manager_rejected_by = rejected_by
        bom.manager_rejected_at = timezone.now()

        # Clear previous approval information.
        bom.manager_approved_by = None
        bom.manager_approved_at = None

        bom.save(
            update_fields=[
                "status",
                "manager_rejection_reason",
                "manager_rejected_by",
                "manager_rejected_at",
                "manager_approved_by",
                "manager_approved_at",
                "updated_at",
            ]
        )

        # Store rejection details in the notification message.
        # Do not use rejection_reason or rejected_by here unless
        # those fields exist in the Notification model.
        update_manager_notification(
            bom=bom,
            notification_status="MANAGER_REJECTED",
            is_read=True,
            title=(
                f"BOM Rejected - "
                f"{bom.bom_number}"
            ),
            message=(
                f"BOM {bom.bom_number} was rejected "
                f"by {rejected_by}. Remarks: {remarks}"
            ),
        )

        transaction.on_commit(invalidate_bom_cache)

        transaction.on_commit(
            lambda bom_id=bom.id,
            manager_name=rejected_by,
            reason=remarks: (
                enqueue_bom_email(
                    send_bom_creator_result_email,
                    bom_id,
                    outcome="rejected",
                    action_by=manager_name,
                    rejection_reason=reason,
                )
            )
        )

        serializer = self.get_serializer(bom)

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )


class BOMItemViewSet(viewsets.ModelViewSet):
    queryset = (
        BOMItem.objects
        .select_related(
            "bom",
            "component",
        )
        .all()
    )

    serializer_class = BOMItemSerializer
    pagination_class = OptionalPageNumberPagination

    def list(self, request, *args, **kwargs):
        version = get_bom_item_cache_version()
        cache_key = None

        if version:
            cache_key = build_list_cache_key(
                "ipms:bom_items:list",
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
                    timeout=BOM_ITEM_LIST_CACHE_TTL_SECONDS,
                )
            except Exception:
                pass

        return response

    def get_queryset(self):
        queryset = super().get_queryset()

        bom_id = (
            self.request.query_params.get(
                "bom"
            )
        )

        if bom_id:
            queryset = queryset.filter(
                bom_id=bom_id
            )

        return apply_server_query_parameters(
            queryset,
            self.request,
            search_fields=(
                "component_code",
                "category",
                "specifications",
                "unit",
                "vendor",
                "remarks",
                "component__component_id",
                "component__name",
            ),
            filter_fields={
                "bom": "bom_id",
                "component": "component_id",
                "category": "category__icontains",
                "vendor": "vendor__icontains",
            },
            ordering_fields=("component_code", "quantity", "category"),
            default_ordering=("id",),
        )

    def ensure_bom_is_editable(self, bom):
        current_status = str(
            bom.status or ""
        ).upper()

        # Pending Manager remains editable based on
        # your stated workflow.
        editable_statuses = {
            "PENDING_MANAGER",
            "MANAGER_REJECTED",
            "MODIFIED",
        }

        if current_status not in editable_statuses:
            raise PermissionDenied(
                (
                    "This BOM cannot be edited while "
                    f"its status is {current_status}."
                )
            )

    @transaction.atomic
    def perform_create(self, serializer):
        invalidate_bom_item_cache()
        bom = serializer.validated_data.get(
            "bom"
        )

        if not bom:
            raise PermissionDenied(
                "BOM ID is required."
            )

        self.ensure_bom_is_editable(bom)

        item = serializer.save()

        mark_bom_as_modified(
            item.bom,
            actor_name=get_actor_name(
                self.request,
                "modified_by",
            ),
        )

        transaction.on_commit(invalidate_bom_cache)

    @transaction.atomic
    def perform_update(self, serializer):
        invalidate_bom_item_cache()
        current_item = self.get_object()

        self.ensure_bom_is_editable(
            current_item.bom
        )

        item = serializer.save()

        mark_bom_as_modified(
            item.bom,
            actor_name=get_actor_name(
                self.request,
                "modified_by",
            ),
        )

        transaction.on_commit(invalidate_bom_cache)

    @transaction.atomic
    def perform_destroy(self, instance):
        invalidate_bom_item_cache()
        bom = instance.bom

        self.ensure_bom_is_editable(bom)

        instance.delete()

        mark_bom_as_modified(
            bom,
            actor_name=get_actor_name(
                self.request,
                "modified_by",
            ),
        )

        transaction.on_commit(invalidate_bom_cache)


class ProjectBOMViewSet(viewsets.ReadOnlyModelViewSet):
    """Saved Custom BOM snapshots, created atomically with their MRs."""

    serializer_class = ProjectBOMSerializer
    # The MR endpoint explicitly uses JWT authentication. Match it here so
    # IsAuthenticated can recognize the token sent by fetchAuthenticatedJson.
    authentication_classes = (JWTAuthentication,)
    permission_classes = (IsAuthenticated,)
    pagination_class = None
    queryset = ProjectBOM.objects.select_related(
        "material_request", "source_bom",
    ).order_by("-created_at", "-id")


def get_approved_master_components():
    """One canonical component per active, approved Product BOM."""
    result = {}
    items = BOMItem.objects.filter(
        bom__status="APPROVED", bom__is_active=True,
    ).select_related("component", "bom").order_by("bom_id", "id")
    for item in items:
        component = item.component
        code = str(
            (component.component_id if component else "")
            or item.component_code or ""
        ).strip()
        if not code:
            continue
        key = code.upper()
        if key not in result:
            result[key] = {
                "component_code": code,
                "component_name": str(getattr(component, "name", "") or ""),
                "category": str(getattr(component, "category", "") or item.category or ""),
                "component_type": str(getattr(component, "component_type", "") or ""),
                "hsn_no": str(getattr(component, "hsn_numbers", "") or ""),
                "specifications": str(
                    getattr(component, "specifications", "") or item.specifications or ""
                ),
                "source_bom_numbers": [],
            }
        if item.bom.bom_number not in result[key]["source_bom_numbers"]:
            result[key]["source_bom_numbers"].append(item.bom.bom_number)
    return result


class MasterBOMViewSet(viewsets.ViewSet):
    """Live distinct approved components with separately saved costing data."""

    authentication_classes = (JWTAuthentication,)
    permission_classes = (IsAuthenticated,)
    pricing_fields = (
        "quantity", "uom", "vendor", "unit_price", "discount",
        "gst_percent", "freight_cost", "freight_gst_percent",
    )

    def _response_row(self, component_row, pricing=None):
        row = dict(component_row)
        for field in self.pricing_fields:
            value = getattr(pricing, field, None)
            row[field] = "" if value is None else str(value)

        qty = getattr(pricing, "quantity", None)
        unit_price = getattr(pricing, "unit_price", None)
        freight_cost = getattr(pricing, "freight_cost", None)
        freight_gst_percent = getattr(pricing, "freight_gst_percent", None)
        freight_gst = (
            freight_cost * freight_gst_percent / 100
            if freight_cost is not None and freight_gst_percent is not None
            else None
        )
        money = lambda value: str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        row["freight_gst_amount"] = "" if freight_gst is None else money(freight_gst)
        if qty is None or unit_price is None:
            row.update(gst_amount="", line_total="")
        else:
            base = qty * unit_price - (pricing.discount or Decimal("0"))
            gst_amount = (
                base * pricing.gst_percent / 100
                if pricing.gst_percent is not None else None
            )
            freight = freight_cost or Decimal("0")
            row.update(
                gst_amount="" if gst_amount is None else money(gst_amount),
                line_total=money(base + (gst_amount or Decimal("0")) + freight + (freight_gst or Decimal("0"))),
            )
        return row

    def list(self, request):
        components = get_approved_master_components()
        prices = {
            item.component_code.upper(): item
            for item in MasterBOMPricing.objects.filter(
                component_code__in=[row["component_code"] for row in components.values()]
            )
        }
        return Response([
            self._response_row(components[key], prices.get(key))
            for key in sorted(components)
        ])

    @action(detail=False, methods=["patch"], url_path="bulk")
    @transaction.atomic
    def bulk_update(self, request):
        """Save the edited Master BOM rows together or roll all of them back."""
        rows = request.data.get("rows")
        if not isinstance(rows, list) or not rows or len(rows) > 1000:
            return Response(
                {"rows": "Provide between 1 and 1000 changed lines."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        components = get_approved_master_components()
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValidationError({"rows": "Each line must be an object."})
            key = str(row.get("component_code") or "").strip().upper()
            if key in seen or key not in components:
                raise ValidationError({"rows": f"Duplicate or unavailable component: {key}"})
            seen.add(key)
            component_row = components[key]
            pricing, _created = MasterBOMPricing.objects.get_or_create(
                component_code=component_row["component_code"],
            )
            serializer = MasterBOMPricingSerializer(
                pricing, data={field: row[field] for field in self.pricing_fields if field in row},
                partial=True,
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            result.append(self._response_row(component_row, pricing))
        return Response(result)

    @transaction.atomic
    def partial_update(self, request, pk=None):
        components = get_approved_master_components()
        component_row = components.get(str(pk or "").strip().upper())
        if component_row is None:
            return Response(
                {"detail": "Component is not in an approved Product BOM."},
                status=status.HTTP_404_NOT_FOUND,
            )
        code = component_row["component_code"]
        pricing, _created = MasterBOMPricing.objects.get_or_create(component_code=code)
        serializer = MasterBOMPricingSerializer(
            pricing, data=request.data, partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(self._response_row(component_row, pricing))
