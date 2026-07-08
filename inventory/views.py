from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Inventory
from .serializers import InventorySerializer


class InventoryViewSet(viewsets.ModelViewSet):
    queryset = Inventory.objects.all().order_by("-created_at")
    serializer_class = InventorySerializer
    pagination_class = None

    @action(detail=False, methods=["get"], url_path="next-code")
    def next_code(self, request):
        last = Inventory.objects.order_by("-id").first()

        if last and last.inventory_code:
            try:
                last_no = int(last.inventory_code.replace("INV", ""))
            except ValueError:
                last_no = 0
        else:
            last_no = 0

        next_code = f"INV{last_no + 1:05d}"

        return Response({
            "inventory_code": next_code
        })