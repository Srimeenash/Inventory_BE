from django.core.cache import cache
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from inventory_backend.cache_utils import (
    build_list_cache_key,
    get_cache_version,
    invalidate_cache_version,
)
from inventory_backend.pagination import (
    OptionalPageNumberPagination,
    apply_server_query_parameters,
)

from .models import ApprovalRequest
from .serializers import ApprovalRequestSerializer

APPROVAL_LIST_CACHE_TTL_SECONDS = 60
APPROVAL_LIST_CACHE_VERSION_KEY = "ipms:approvals:list:version"


def get_approval_cache_version():
    return get_cache_version(APPROVAL_LIST_CACHE_VERSION_KEY)


def invalidate_approval_cache():
    invalidate_cache_version(APPROVAL_LIST_CACHE_VERSION_KEY)


class ApprovalRequestViewSet(viewsets.ModelViewSet):
    queryset = ApprovalRequest.objects.all().order_by('-created_at')
    serializer_class = ApprovalRequestSerializer
    pagination_class = OptionalPageNumberPagination

    def get_queryset(self):
        return apply_server_query_parameters(
            super().get_queryset(),
            self.request,
            search_fields=(
                "module",
                "requested_by",
                "status",
                "remarks",
            ),
            filter_fields={
                "module": "module__iexact",
                "status": "status__iexact",
                "requested_by": "requested_by__icontains",
                "reference_id": "reference_id",
            },
            ordering_fields=(
                "module",
                "reference_id",
                "requested_by",
                "status",
                "created_at",
                "updated_at",
            ),
            default_ordering=("-created_at", "-id"),
        )

    def list(self, request, *args, **kwargs):
        version = get_approval_cache_version()
        cache_key = None

        if version:
            cache_key = build_list_cache_key(
                "ipms:approvals:list",
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
                    timeout=APPROVAL_LIST_CACHE_TTL_SECONDS,
                )
            except Exception:
                pass

        return response

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        approval = self.get_object()
        approval.status = 'APPROVED'
        approval.approved_by = request.user
        approval.save()
        return Response({"message": "Approved successfully"})

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        approval = self.get_object()
        approval.status = 'REJECTED'
        approval.approved_by = request.user
        approval.save()
        return Response({"message": "Rejected successfully"})