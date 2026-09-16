from uuid import uuid4

from django.core.cache import cache
from django.db import transaction

from django_filters.rest_framework import (
    DjangoFilterBackend,
)

from rest_framework import (
    filters,
    status,
    viewsets,
)

from rest_framework.permissions import (
    AllowAny,
)

from rest_framework.response import (
    Response,
)

from inventory_backend.cache_utils import (
    build_list_cache_key,
    get_cache_version,
    invalidate_cache_version,
)
from inventory_backend.pagination import (
    OptionalPageNumberPagination,
    apply_server_query_parameters,
)

from .models import Vendor
from .serializers import VendorSerializer


# ==========================================================
# REDIS / MEMURAI CACHE
# ==========================================================

VENDOR_LIST_CACHE_TTL_SECONDS = 60

VENDOR_LIST_CACHE_VERSION_KEY = (
    "ipms:vendors:list:version"
)


def get_vendor_cache_version():
    """Backward-compatible wrapper for cache version lookups."""
    return get_cache_version(VENDOR_LIST_CACHE_VERSION_KEY)


def invalidate_vendor_cache():
    """Backward-compatible wrapper for invalidating vendor list cache."""
    invalidate_cache_version(VENDOR_LIST_CACHE_VERSION_KEY)


# ==========================================================
# VENDOR VIEWSET
# ==========================================================

class VendorViewSet(
    viewsets.ModelViewSet
):

    """
    Vendor API.

    Products are prefetched to prevent one
    additional products query per Vendor.

    Redis is used only for GET list responses.
    """

    queryset = (
        Vendor.objects

        .filter(
            is_active=True
        )

        .prefetch_related(
            "products"
        )

        .order_by(
            "name"
        )
    )

    serializer_class = (
        VendorSerializer
    )

    permission_classes = [
        AllowAny
    ]

    authentication_classes = []

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_fields = [
        "is_active"
    ]

    search_fields = [
        "vendor_id",
        "name",
        "gst_number",
        "pan_number",
        "contact_person",
        "email",
        "phone_number",
        "address",
        "city",
        "state",
        "state_code",
        "pincode",
        "payment_terms",
        "shipping_terms",
        "additional_notes",
    ]

    ordering_fields = [
        "name",
        "rating",
        "created_at",
    ]

    ordering = [
        "name"
    ]

    pagination_class = (
        OptionalPageNumberPagination
    )

    # ======================================================
    # QUERYSET
    # ======================================================

    def get_queryset(self):

        return apply_server_query_parameters(

            super().get_queryset(),

            self.request,

            search_fields=tuple(
                self.search_fields
            ),

            filter_fields={

                "is_active":
                    "is_active",

                "name":
                    "name__icontains",
            },

            boolean_fields=(
                "is_active",
            ),

            ordering_fields=(
                "name",
                "rating",
                "created_at",
            ),

            default_ordering=(
                "name",
                "id",
            ),
        )

    # ======================================================
    # LIST WITH REDIS CACHE
    # ======================================================

    def list(
        self,
        request,
        *args,
        **kwargs,
    ):

        cache_version = get_vendor_cache_version()
        cache_key = None

        if cache_version:
            cache_key = build_list_cache_key(
                "ipms:vendors:list",
                cache_version,
                request,
            )

            try:
                cached_data = cache.get(cache_key)
            except Exception:
                cached_data = None

            if cached_data is not None:
                return Response(cached_data)

        # --------------------------------------------------
        # CACHE MISS -> MYSQL
        # --------------------------------------------------

        response = super().list(
            request,
            *args,
            **kwargs,
        )

        # --------------------------------------------------
        # SAVE RESULT IN REDIS
        # --------------------------------------------------

        if (
            cache_key
            and response.status_code == 200
        ):

            try:

                cache.set(
                    cache_key,
                    response.data,
                    timeout=(
                        VENDOR_LIST_CACHE_TTL_SECONDS
                    ),
                )

            except Exception:

                pass

        return response

    # ======================================================
    # CREATE
    # ======================================================

    def perform_create(
        self,
        serializer,
    ):

        super().perform_create(
            serializer
        )

        transaction.on_commit(
            invalidate_vendor_cache
        )

    # ======================================================
    # UPDATE
    # ======================================================

    def perform_update(
        self,
        serializer,
    ):

        super().perform_update(
            serializer
        )

        transaction.on_commit(
            invalidate_vendor_cache
        )

    # ======================================================
    # DELETE / SOFT DELETE
    # ======================================================

    def destroy(
        self,
        request,
        *args,
        **kwargs,
    ):

        vendor = (
            self.get_object()
        )

        vendor.is_active = False

        vendor.save(
            update_fields=[
                "is_active"
            ]
        )

        transaction.on_commit(
            invalidate_vendor_cache
        )

        return Response(
            status=(
                status.HTTP_204_NO_CONTENT
            )
        )