from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, viewsets
from rest_framework.permissions import AllowAny

from .models import Component
from .serializers import ComponentSerializer


class ComponentViewSet(viewsets.ModelViewSet):
    """
    Single Component ViewSet used by the Sales/Event In-Store dropdown.

    The earlier file declared ComponentViewSet twice. The second declaration
    replaced the first one and silently removed filtering, search, ordering,
    authentication and permissions.
    """

    queryset = Component.objects.all().order_by("component_id")
    serializer_class = ComponentSerializer

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["is_active", "category"]
    search_fields = ["component_id", "name", "specifications"]
    ordering_fields = [
        "component_id",
        "name",
        "stock_quantity",
        "unit_price",
    ]
    ordering = ["component_id"]

    # Preserve the project's existing public Component endpoint behaviour.
    authentication_classes = []
    permission_classes = [AllowAny]