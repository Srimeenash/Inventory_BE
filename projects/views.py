from uuid import uuid4

from django.core.cache import cache
from django.db import transaction

from django_filters.rest_framework import DjangoFilterBackend

from rest_framework import filters, viewsets
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

from .models import Project
from .serializers import ProjectSerializer


# ==========================================================
# REDIS / MEMURAI CACHE
# ==========================================================

PROJECT_LIST_CACHE_TTL_SECONDS = 60

PROJECT_LIST_CACHE_VERSION_KEY = (
    "ipms:projects:list:version"
)


def get_project_cache_version():
    """Backward-compatible wrapper for project cache version lookups."""
    return get_cache_version(PROJECT_LIST_CACHE_VERSION_KEY)


def invalidate_project_cache():
    """Backward-compatible wrapper for invalidating project list cache."""
    invalidate_cache_version(PROJECT_LIST_CACHE_VERSION_KEY)


# ==========================================================
# PROJECT VIEWSET
# ==========================================================

class ProjectViewSet(
    viewsets.ModelViewSet
):

    """
    Project API.

    Team is prefetched so serializer does not
    create one extra query for each Project.

    Redis is used only for GET list responses.
    """

    queryset = (
        Project.objects
        .prefetch_related(
            "team"
        )
        .order_by(
            "-created_at"
        )
    )

    serializer_class = (
        ProjectSerializer
    )

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_fields = [
        "is_active",
        "status",
        "department",
    ]

    search_fields = [
        "project_code",
        "name",
        "description",
        "department",
    ]

    ordering_fields = [
        "project_code",
        "name",
        "start_date",
        "end_date",
        "created_at",
    ]

    ordering = [
        "-created_at"
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

                "status":
                    "status__iexact",

                "department":
                    "department__icontains",

                "project_type":
                    "project_type__icontains",
            },

            boolean_fields=(
                "is_active",
            ),

            ordering_fields=tuple(
                self.ordering_fields
            ),

            default_ordering=(
                "-created_at",
                "-id",
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

        cache_version = get_project_cache_version()
        cache_key = None

        if cache_version:
            cache_key = build_list_cache_key(
                "ipms:projects:list",
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
                        PROJECT_LIST_CACHE_TTL_SECONDS
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
            invalidate_project_cache
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
            invalidate_project_cache
        )

    # ======================================================
    # DELETE
    # ======================================================

    def perform_destroy(
        self,
        instance,
    ):

        super().perform_destroy(
            instance
        )

        transaction.on_commit(
            invalidate_project_cache
        )