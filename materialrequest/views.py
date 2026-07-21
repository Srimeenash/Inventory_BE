from rest_framework import viewsets
from .models import MaterialRequest
from .serializers import MaterialRequestSerializer
from notifications.models import Notification


class MaterialRequestViewSet(viewsets.ModelViewSet):
    queryset = MaterialRequest.objects.all().order_by("-date")
    serializer_class = MaterialRequestSerializer
    pagination_class = None

    def perform_create(self, serializer):
        mr = serializer.save()   # <-- Store the saved Material Request

        Notification.objects.create(
            category="MR",
            title=f"MR Approval Request - {mr.material_request_id}",
            message=f"Approval requested for MR {mr.material_request_id}",
            reference_id=mr.id,
            status="REQUESTED",
            receiver="ADMIN",
        )
    def perform_update(self, serializer):
        old = self.get_object()

        old_status = old.status
        old_approval = old.approval_status

        mr = serializer.save()
# When moved to Procurement
# When moved to Procurement
        if mr.status == "PO_RAISED":

            # Delete ALL MR notifications for this Material Request
            Notification.objects.filter(
                category="MR",
                reference_id=mr.id,
            ).delete()

            # Create Procurement notification only once
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
                )
                # Admin approved -> send notification to Manager
        if (
            old_approval != "ADMIN_APPROVED"
            and mr.approval_status == "ADMIN_APPROVED"
        ):
            Notification.objects.create(
                category="MR",
                title=f"MR Approval Request - {mr.material_request_id}",
                message=f"Approval requested for MR {mr.material_request_id}",
                reference_id=mr.id,
                status="PENDING_MANAGER",
                receiver="MANAGER",
            )

        # Manager approved -> update notification
        if (
            old_approval != "MANAGER_APPROVED"
            and mr.approval_status == "MANAGER_APPROVED"
        ):
            Notification.objects.filter(
                category="MR",
                reference_id=mr.id,
                receiver="MANAGER",
            ).update(
                status="APPROVED",
                is_read=True,
            )

        # Rejected
        if mr.status == "REJECTED":
            Notification.objects.filter(
                category="MR",
                reference_id=mr.id,
            ).update(
                status="REJECTED",
                is_read=True,
            )