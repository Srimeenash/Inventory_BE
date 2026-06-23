from rest_framework.viewsets import ModelViewSet
from .models import ComponentUsage
from .serializers import ComponentUsageSerializer

class ComponentUsageViewSet(ModelViewSet):
    queryset = ComponentUsage.objects.all().order_by('-id')
    serializer_class = ComponentUsageSerializer