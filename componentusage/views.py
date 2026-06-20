from rest_framework import viewsets
from .models import ComponentUsage
from .serializers import ComponentUsageSerializer

class ComponentUsageViewSet(viewsets.ModelViewSet):
    queryset = ComponentUsage.objects.all().order_by("-date")
    serializer_class = ComponentUsageSerializer
