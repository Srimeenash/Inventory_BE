from rest_framework import filters, viewsets
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
