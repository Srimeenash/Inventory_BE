from rest_framework import filters, viewsets
from django_filters.rest_framework import DjangoFilterBackend
from .models import Vendor
from .serializers import VendorSerializer
from accounts.permissions import IsManager


class VendorViewSet(viewsets.ModelViewSet):
    queryset = Vendor.objects.all().order_by('name')
    serializer_class = VendorSerializer
    permission_classes = [IsManager]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['name', 'gst_number', 'pan_number', 'contact_person', 'email', 'phone']
    ordering_fields = ['name', 'rating', 'created_at']
    ordering = ['name']
