from django.core.cache import cache
from rest_framework import filters, viewsets
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from inventory_backend.cache_utils import (
    build_list_cache_key,
    get_cache_version,
    invalidate_cache_version,
)
from inventory_backend.pagination import (
    OptionalPageNumberPagination,
    apply_server_query_parameters,
)
from .models import Role
from .serializers import RoleSerializer

ROLE_LIST_CACHE_TTL_SECONDS = 60
ROLE_LIST_CACHE_VERSION_KEY = "ipms:roles:list:version"


def get_role_cache_version():
    return get_cache_version(ROLE_LIST_CACHE_VERSION_KEY)


def invalidate_role_cache():
    invalidate_cache_version(ROLE_LIST_CACHE_VERSION_KEY)


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all().order_by('name')
    serializer_class = RoleSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'name']
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'created_at']
    ordering = ['name']
    pagination_class = OptionalPageNumberPagination

    def get_queryset(self):
        return apply_server_query_parameters(
            super().get_queryset(),
            self.request,
            search_fields=("name", "description"),
            filter_fields={
                "is_active": "is_active",
                "name": "name__icontains",
            },
            boolean_fields=("is_active",),
            ordering_fields=("name", "created_at"),
            default_ordering=("name", "id"),
        )

    def list(self, request, *args, **kwargs):
        version = get_role_cache_version()
        cache_key = None

        if version:
            cache_key = build_list_cache_key(
                "ipms:roles:list",
                version,
                request,
            )
            try:
                cached_data = cache.get(cache_key)
            except Exception:
                cached_data = None
            if cached_data is not None:
                return Response(cached_data)

        response = super().list(request, *args, **kwargs)

        if cache_key and response.status_code == 200:
            try:
                cache.set(
                    cache_key,
                    response.data,
                    timeout=ROLE_LIST_CACHE_TTL_SECONDS,
                )
            except Exception:
                pass

        return response
