from django.db import transaction
from django.utils import timezone

from notifications.models import Notification

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from .models import BOM, BOMItem
from .serializers import (
    BOMItemSerializer,
    BOMSerializer,
)


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
            full_name
            or getattr(user, "username", "")
            or getattr(user, "email", "")
            or "MANAGER"
        )

    return (
        request.data.get(payload_field)
        or request.data.get("manager_name")
        or "MANAGER"
    )


def update_manager_notification(
    bom,
    notification_status,
    is_read,
    title=None,
    message=None,
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
        )


def mark_bom_as_modified(bom):
    """
    Change a Manager Rejected BOM to Modified and
    send it back to the Manager as an unread request.
    """

    current_status = str(
        bom.status or ""
    ).upper()

    if current_status not in {
        "MANAGER_REJECTED",
        "MODIFIED",
    }:
        return

    if current_status != "MODIFIED":
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


class BOMViewSet(viewsets.ModelViewSet):
    serializer_class = BOMSerializer

    def get_queryset(self):
        queryset = (
            BOM.objects
            .prefetch_related(
                "items",
                "items__component",
            )
            .all()
            .order_by("-created_at")
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

        return queryset

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

        return queryset

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
            item.bom
        )

    @transaction.atomic
    def perform_update(self, serializer):
        current_item = self.get_object()

        self.ensure_bom_is_editable(
            current_item.bom
        )

        item = serializer.save()

        mark_bom_as_modified(
            item.bom
        )

    @transaction.atomic
    def perform_destroy(self, instance):
        bom = instance.bom

        self.ensure_bom_is_editable(bom)

        instance.delete()

        mark_bom_as_modified(bom)