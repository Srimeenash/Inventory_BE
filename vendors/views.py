from rest_framework import filters, viewsets, status
from rest_framework.permissions import AllowAny
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from .models import Vendor
from .serializers import VendorSerializer


class VendorViewSet(viewsets.ModelViewSet):
    queryset = Vendor.objects.filter(is_active=True).prefetch_related("products").all().order_by("name")
    serializer_class = VendorSerializer

    permission_classes = [AllowAny]
    authentication_classes = []

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_fields = ["is_active"]

    search_fields = [
        "name",
        "gst_number",
        "pan_number",
        "contact_person",
        "email",
        "phone",
    ]

    ordering_fields = [
        "name",
        "rating",
        "created_at",
    ]

    ordering = ["name"]

    def destroy(self, request, *args, **kwargs):
        """Soft delete: deactivate vendor instead of hard delete"""
        vendor = self.get_object()
        vendor.is_active = False
        vendor.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def list(self, request, *args, **kwargs):
        print("VENDOR VIEWSET CALLED")
        return super().list(request, *args, **kwargs)