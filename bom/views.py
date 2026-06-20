from rest_framework import viewsets
from .models import BOM, BOMItem
from .serializers import BOMSerializer, BOMItemSerializer


class BOMViewSet(viewsets.ModelViewSet):
    queryset = BOM.objects.all().order_by('-created_at')
    serializer_class = BOMSerializer


class BOMItemViewSet(viewsets.ModelViewSet):
    queryset = BOMItem.objects.all()
    serializer_class = BOMItemSerializer

    def get_queryset(self):
        queryset = BOMItem.objects.all()

        bom_id = self.request.query_params.get("bom")

        if bom_id:
            queryset = queryset.filter(bom_id=bom_id)

        return queryset