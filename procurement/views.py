from rest_framework import viewsets
from .models import PurchaseRequest, PurchaseOrder
from .serializers import PurchaseRequestSerializer, PurchaseOrderSerializer
from notifications.models import Notification


from rest_framework.permissions import AllowAny

class PurchaseRequestViewSet(viewsets.ModelViewSet):
    queryset = PurchaseRequest.objects.all().order_by('-created_at')
    serializer_class = PurchaseRequestSerializer
    permission_classes = [AllowAny]


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    queryset = PurchaseOrder.objects.all().order_by("-created_at")
    serializer_class = PurchaseOrderSerializer
    permission_classes = [AllowAny]

    def perform_update(self, serializer):
        old = self.get_object()
        old_status = old.approval_status

        po = serializer.save()

        # ---------------- Pending Finance ----------------
        if (
            old_status != "PENDING_FINANCE"
            and po.approval_status == "PENDING_FINANCE"
        ):
            Notification.objects.filter(
                category="PO",
                reference_id=po.id,
                receiver="FINANCE",
            ).delete()

            Notification.objects.create(
                category="PO",
                title=f"PO Approval Request - {po.po_number}",
                message=f"Approval requested for PO {po.po_number}",
                reference_id=po.id,
                status="PENDING_FINANCE",
                receiver="FINANCE",
                is_read=False,
            )

        # ---------------- Finance Approved ----------------
        elif (
            old_status != "FINANCE_APPROVED"
            and po.approval_status == "FINANCE_APPROVED"
        ):
            Notification.objects.filter(
                category="PO",
                reference_id=po.id,
                receiver="FINANCE",
            ).update(
                status="FINANCE_APPROVED",
                is_read=True,
            )

            po.status = "FINANCE_APPROVED"
            po.approval_status = "FINANCE_APPROVED"
            po.save(update_fields=["status", "approval_status"])

        # ---------------- Finance Rejected ----------------
        elif (
            old_status != "FINANCE_REJECTED"
            and po.approval_status == "FINANCE_REJECTED"
        ):
            Notification.objects.filter(
                category="PO",
                reference_id=po.id,
                receiver="FINANCE",
            ).update(
                status="FINANCE_REJECTED",
                is_read=True,
            )

            po.status = "FINANCE_REJECTED"
            po.approval_status = "FINANCE_REJECTED"
            po.save(update_fields=["status", "approval_status"])
    queryset = PurchaseOrder.objects.all().order_by("-created_at")
    serializer_class = PurchaseOrderSerializer
    permission_classes = [AllowAny]

    def perform_update(self, serializer):
        old = self.get_object()

        old_status = old.approval_status

        po = serializer.save()

        # Finance approval requested
        if (
            old_status != "PENDING_FINANCE"
            and po.approval_status == "PENDING_FINANCE"
        ):

            Notification.objects.filter(
                category="PO",
                reference_id=po.id,
                receiver="FINANCE",
            ).delete()

            Notification.objects.create(
                category="PO",
                title=f"PO Approval Request - {po.po_number}",
                message=f"Approval requested for PO {po.po_number}",
                reference_id=po.id,
                status="PENDING_FINANCE",
                receiver="FINANCE",
                is_read=False,
            )

        # Finance approved
        if (
            old_status != "FINANCE_APPROVED"
            and po.approval_status == "FINANCE_APPROVED"
        ):

            Notification.objects.filter(
                category="PO",
                reference_id=po.id,
                receiver="FINANCE",
            ).update(
                status="FINANCE_APPROVED",
                is_read=True,
            )

            po.status = "FINANCE_APPROVED"
            po.approval_status = "FINANCE_APPROVED"
            po.save(update_fields=["status", "approval_status"])

                    # Finance rejected
# Finance rejected
            if (
                old_status != "FINANCE_REJECTED"
                and po.approval_status == "FINANCE_REJECTED"
            ):
                # Update notification
                Notification.objects.filter(
                    category="PO",
                    reference_id=po.id,
                    receiver="FINANCE",
                ).update(
                    status="FINANCE_REJECTED",
                    is_read=True,
                )

                # Update PO status
                po.status = "FINANCE_REJECTED"
                po.approval_status = "FINANCE_REJECTED"
                po.save(update_fields=["status", "approval_status"])