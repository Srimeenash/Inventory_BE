from rest_framework import viewsets
from .models import MaterialRequest
from .serializers import MaterialRequestSerializer
from notifications.models import Notification


class MaterialRequestViewSet(viewsets.ModelViewSet):
    queryset = MaterialRequest.objects.all().order_by("-date")
    serializer_class = MaterialRequestSerializer
    pagination_class = None

    # -------------------------------------------------
    # Create Material Request
    # -------------------------------------------------
    def perform_create(self, serializer):
        # Only save the MR
        # DO NOT create any notification here
        serializer.save()

    # -------------------------------------------------
    # Update Material Request
    # -------------------------------------------------
    def perform_update(self, serializer):


        old = self.get_object()

        old_status = old.status
        old_approval = old.approval_status

        mr = serializer.save()

        # =============================================
        # Engineer clicked Request Approval
        # =============================================
        if (
            old_approval != "PENDING_MANAGER"
            and mr.approval_status == "PENDING_MANAGER"
        ):

            Notification.objects.filter(
                category="MR",
                reference_id=mr.id,
            ).delete()

            Notification.objects.create(
                category="MR",
                title=f"MR Approval Request - {mr.material_request_id}",
                message=f"Approval requested for MR {mr.material_request_id}",
                reference_id=mr.id,
                status="PENDING_MANAGER",
                receiver="MANAGER",
                is_read=False,
            )

        # =============================================
        # Move to Procurement
        # =============================================
        if mr.status == "PO_RAISED":

            Notification.objects.filter(
                category="MR",
                reference_id=mr.id,
            ).delete()

            if not Notification.objects.filter(
                category="PROC",
                reference_id=mr.id,
                receiver="PROCUREMENT",
            ).exists():

                Notification.objects.create(
                    category="PROC",
                    title=f"New Procurement Request - {mr.material_request_id}",
                    message=f"{mr.material_request_id} has been moved to Procurement.",
                    reference_id=mr.id,
                    status="REQUESTED",
                    receiver="PROCUREMENT",
                    is_read=False,
                )

        # =============================================
        # Manager Approved
        # =============================================
# =============================================
# Manager Approved
# =============================================
# =============================================
# Manager Approved
# =============================================

        if (
            mr.status == "MANAGER_APPROVED"
            or mr.approval_status == "MANAGER_APPROVED"
        ):

            mr.status = "MANAGER_APPROVED"
            mr.approval_status = "MANAGER_APPROVED"

            mr.save(
                update_fields=[
                    "status",
                    "approval_status"
                ]
            )

            Notification.objects.filter(
                category="MR",
                reference_id=mr.id,
                receiver="MANAGER",
            ).update(
                status="MANAGER_APPROVED",
                is_read=True,
            )

            Notification.objects.create(
                category="MR",
                title=f"MR Approved - {mr.material_request_id}",
                message=f"{mr.material_request_id} approved by manager and ready for inventory.",
                reference_id=mr.id,
                status="MANAGER_APPROVED",
                receiver="INVENTORY",
                is_read=False,
            )

        # =============================================
        # Rejected
        # =============================================
        # =============================================
        # Manager Rejected
        # =============================================
        # =============================================
        # Manager Rejected
        # =============================================
# =============================================
# Manager Rejected
# =============================================
        if (
            mr.status == "MANAGER_REJECTED"
            or mr.approval_status == "MANAGER_REJECTED"
            or mr.status == "REJECTED"
            or mr.approval_status == "REJECTED"
        ):

            mr.status = "MANAGER_REJECTED"
            mr.approval_status = "MANAGER_REJECTED"

            mr.save(
                update_fields=[
                    "status",
                    "approval_status"
                ]
            )


            # Update manager notification
            Notification.objects.filter(
                category="MR",
                reference_id=mr.id,
                receiver="MANAGER",
            ).update(
                status="MANAGER_REJECTED",
                is_read=True,
            )