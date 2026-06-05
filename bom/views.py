from rest_framework import viewsets
from .models import BOM
from .serializers import BOMSerializer
from accounts.permissions import IsManager


class BOMViewSet(viewsets.ModelViewSet):
    queryset = BOM.objects.all().order_by('-created_at')
    serializer_class = BOMSerializer
    permission_classes = [IsManager]