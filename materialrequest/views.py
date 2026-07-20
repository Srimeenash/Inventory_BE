from rest_framework import viewsets
from .models import MaterialRequest
from .serializers import MaterialRequestSerializer
from notifications.models import Notification

class MaterialRequestViewSet(viewsets.ModelViewSet):
    queryset = MaterialRequest.objects.all().order_by("-date")
    serializer_class = MaterialRequestSerializer
    pagination_class = None

    def perform_create(self, serializer):
        instance = serializer.save()

        if self.request.user and self.request.user.is_authenticated and instance.approval_status == "REQUESTED":
            shortage_items = []
            for item in instance.bom_items.all():
                if item.quantity > item.inventory_quantity:
                    shortage_items.append(item.component.component_id if item.component else item.component_id or str(item.id))
            for item in instance.rd_items.all():
                if item.quantity > item.inventory_quantity:
                    shortage_items.append(item.component.component_id if item.component else str(item.id))

            details = " and ".join(shortage_items) if shortage_items else "components"
            Notification.objects.create(
                category="MR",
                title=f"MR {instance.material_request_id} requires procurement review",
                message=f"Material request {instance.material_request_id} contains shortage items: {details}.",
                reference_id=str(instance.id),
                status="PENDING",
                is_read=False,
            )

            