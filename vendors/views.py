from rest_framework import filters, viewsets
from rest_framework.permissions import AllowAny
from django_filters.rest_framework import DjangoFilterBackend

from .models import Vendor
from .serializers import VendorSerializer


class VendorViewSet(viewsets.ModelViewSet):
    queryset = Vendor.objects.prefetch_related("products").all().order_by("name")
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

    def list(self, request, *args, **kwargs):
        print("VENDOR VIEWSET CALLED")
        return super().list(request, *args, **kwargs)