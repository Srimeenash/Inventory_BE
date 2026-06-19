from rest_framework import viewsets, filters
from rest_framework.permissions import AllowAny
from django_filters.rest_framework import DjangoFilterBackend
from .models import Component
from .serializers import ComponentSerializer

class ComponentViewSet(viewsets.ModelViewSet):
    queryset = Component.objects.all().order_by('component_id')
    serializer_class = ComponentSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'category']
    search_fields = ['component_id', 'name', 'specifications']
    ordering_fields = ['component_id', 'name', 'stock_quantity', 'unit_price']
    ordering = ['component_id']

    # 👇 disable JWT for this endpoint only
    authentication_classes = []        # disables global JWT requirement
    permission_classes = [AllowAny]    # makes it public
    
class ComponentViewSet(viewsets.ModelViewSet):   # ✅ change this
    queryset = Component.objects.all()
    serializer_class = ComponentSerializer