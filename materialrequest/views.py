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
        if (
            old_approval != "MANAGER_APPROVED"
            and mr.approval_status == "MANAGER_APPROVED"
        ):
            # Update MR status also
            mr.status = "MANAGER_APPROVED"
            mr.save(update_fields=["status"])

            Notification.objects.filter(
                category="MR",
                reference_id=mr.id,
                receiver="MANAGER",
            ).update(
                status="MANAGER_APPROVED",
                is_read=True,
            )

        # =============================================
        # Rejected
        # =============================================
        if mr.status == "REJECTED":

            Notification.objects.filter(
                category="MR",
                reference_id=mr.id,
            ).update(
                status="REJECTED",
                is_read=True,
            )

