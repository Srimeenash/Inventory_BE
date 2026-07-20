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

        shortage_items = []

        # BOM Items
        for item in instance.bom_items.all():
            if item.quantity > item.inventory_quantity:
                shortage_items.append(
                    item.component.component_id if item.component else str(item.id)
                )

        # R&D Items
        for item in instance.rd_items.all():
            if item.quantity > item.inventory_quantity:
                shortage_items.append(
                    item.component.component_id if item.component else str(item.id)
                )

        # Notify Procurement only if shortage exists
        if shortage_items:
            Notification.objects.create(
                category="PO",
                title=f"Procurement Required - {instance.material_request_id}",
                message=(
                    f"Inventory is insufficient for "
                    f"{', '.join(shortage_items)}. "
                    "Please raise a Purchase Order."
                ),
                reference_id=str(instance.id),
                status="PENDING",
                is_read=False,
            )