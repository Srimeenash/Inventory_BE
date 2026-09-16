from uuid import uuid4

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q

from django_filters.rest_framework import DjangoFilterBackend

from rest_framework import filters, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from inventory_backend.pagination import (
    OptionalPageNumberPagination,
    apply_server_query_parameters,
)

from .models import Component
from .serializers import (
    ComponentLookupSerializer,
    ComponentSerializer,
)


# ---------------------------------------------------------------------
# REDIS / MEMURAI CACHE SETTINGS
# ---------------------------------------------------------------------

COMPONENT_LOOKUP_CACHE_TTL_SECONDS = 60

COMPONENT_LOOKUP_VERSION_KEY = (
    "ipms:components:lookup:version"
)


def get_component_lookup_cache_version():
    """
    Return the current Component lookup cache version.

    If Redis/Memurai is unavailable, the normal MySQL API
    continues working.
    """

    try:
        version = cache.get(
            COMPONENT_LOOKUP_VERSION_KEY
        )

        if version is None:
            version = uuid4().hex

            cache.set(
                COMPONENT_LOOKUP_VERSION_KEY,
                version,
                timeout=None,
            )

        return str(version)

    except Exception:
        return None


def invalidate_component_lookup_cache():
    """
    Invalidate all existing Component lookup cache entries.

    Instead of deleting Redis keys using wildcard operations,
    change the namespace version.

    Existing old keys expire automatically after 60 seconds.
    """

    try:
        cache.set(
            COMPONENT_LOOKUP_VERSION_KEY,
            uuid4().hex,
            timeout=None,
        )

    except Exception:
        # Redis is only a performance optimization.
        # Redis failure must never block Component updates.
        pass


class ComponentViewSet(viewsets.ModelViewSet):

    """
    Component API.

    Existing behaviour is preserved:

    - search
    - filters
    - ordering
    - pagination
    - create
    - update
    - delete

    Redis/Memurai caching is used ONLY for:

        /lookup/

    Main Component list/detail APIs remain live.
    """

    queryset = (
        Component.objects
        .all()
        .order_by(
            "component_id"
        )
    )

    serializer_class = (
        ComponentSerializer
    )

    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]

    filterset_fields = [
        "is_active",
        "category",
        "component_type",
    ]

    search_fields = [
        "component_id",
        "name",
        "component_type",
        "specifications",
    ]

    ordering_fields = [
        "component_id",
        "name",
        "stock_quantity",
        "unit_price",
    ]

    ordering = [
        "component_id"
    ]

    pagination_class = (
        OptionalPageNumberPagination
    )

    authentication_classes = []

    permission_classes = [
        AllowAny
    ]

    # -----------------------------------------------------------------
    # QUERYSET
    # -----------------------------------------------------------------

    def get_queryset(self):

        return apply_server_query_parameters(

            super().get_queryset(),

            self.request,

            search_fields=(
                "component_id",
                "name",
                "component_type",
                "specifications",
            ),

            filter_fields={

                "is_active":
                    "is_active",

                "category":
                    "category__iexact",

                "component_type":
                    "component_type__iexact",
            },

            boolean_fields=(
                "is_active",
            ),

            ordering_fields=(
                "component_id",
                "name",
                "stock_quantity",
                "unit_price",
            ),

            default_ordering=(
                "component_id",
                "id",
            ),
        )

    # -----------------------------------------------------------------
    # CREATE
    # -----------------------------------------------------------------

    def perform_create(
        self,
        serializer,
    ):

        super().perform_create(
            serializer
        )

        # Clear lookup cache only after DB commit.
        transaction.on_commit(
            invalidate_component_lookup_cache
        )

    # -----------------------------------------------------------------
    # UPDATE
    # -----------------------------------------------------------------

    def perform_update(
        self,
        serializer,
    ):

        super().perform_update(
            serializer
        )

        transaction.on_commit(
            invalidate_component_lookup_cache
        )

    # -----------------------------------------------------------------
    # DELETE
    # -----------------------------------------------------------------

    def perform_destroy(
        self,
        instance,
    ):

        super().perform_destroy(
            instance
        )

        transaction.on_commit(
            invalidate_component_lookup_cache
        )

    # -----------------------------------------------------------------
    # COMPONENT LOOKUP
    # -----------------------------------------------------------------

    @action(
        detail=False,
        methods=["get"],
        url_path="lookup",
    )
    def lookup(
        self,
        request,
    ):

        """
        Fast Component dropdown endpoint.

        Examples:

        /api/components/components/lookup/

        /api/components/components/lookup/?search=wing

        /api/components/components/lookup/?search=wing&page_size=20


        Redis behaviour
        ---------------

        First request:

            Redis miss
                ↓
            MySQL query
                ↓
            Redis save
                ↓
            Response

        Next request:

            Redis hit
                ↓
            Response

        Cache lifetime:

            60 seconds

        Component create/update/delete automatically
        invalidates the lookup cache.
        """

        # -------------------------------------------------------------
        # SEARCH VALUE
        # -------------------------------------------------------------

        search = str(

            request.query_params.get(
                "search",
                "",
            )

            or ""

        ).strip()

        # -------------------------------------------------------------
        # PAGE SIZE
        # -------------------------------------------------------------

        try:

            page_size = int(

                request.query_params.get(
                    "page_size",
                    20,
                )

            )

        except (
            TypeError,
            ValueError,
        ):

            page_size = 20

        page_size = max(

            1,

            min(
                page_size,
                50,
            ),
        )

        # -------------------------------------------------------------
        # REDIS CACHE VERSION
        # -------------------------------------------------------------

        cache_version = (
            get_component_lookup_cache_version()
        )

        cache_key = None

        # -------------------------------------------------------------
        # CHECK REDIS
        # -------------------------------------------------------------

        if cache_version:

            cache_key = (

                "ipms:components:lookup:"

                f"{cache_version}:"

                f"{search.casefold()}:"

                f"{page_size}"
            )

            try:

                cached_data = cache.get(
                    cache_key
                )

            except Exception:

                cached_data = None

            # ---------------------------------------------------------
            # CACHE HIT
            # ---------------------------------------------------------

            if cached_data is not None:

                return Response(
                    cached_data
                )

        # -------------------------------------------------------------
        # CACHE MISS → MYSQL
        # -------------------------------------------------------------

        queryset = (

            Component.objects

            .filter(
                is_active=True
            )

            .only(
                "id",
                "component_id",
                "name",
                "category",
                "component_type",
                "unit_of_measurements",
                "is_active",
            )

            .order_by(
                "component_id"
            )
        )

        # -------------------------------------------------------------
        # SEARCH
        # -------------------------------------------------------------

        if search:

            queryset = queryset.filter(

                Q(
                    component_id__icontains=
                    search
                )

                |

                Q(
                    name__icontains=
                    search
                )

                |

                Q(
                    component_type__icontains=
                    search
                )
            )

        # -------------------------------------------------------------
        # SERIALIZE
        # -------------------------------------------------------------

        serializer = (
            ComponentLookupSerializer(

                queryset[:page_size],

                many=True,

                context={
                    "request": request,
                },
            )
        )

        data = serializer.data

        # -------------------------------------------------------------
        # SAVE TO REDIS
        # -------------------------------------------------------------

        if cache_key:

            try:

                cache.set(

                    cache_key,

                    data,

                    timeout=(
                        COMPONENT_LOOKUP_CACHE_TTL_SECONDS
                    ),
                )

            except Exception:

                # Redis failure must never break API.
                pass

        # -------------------------------------------------------------
        # RESPONSE
        # -------------------------------------------------------------

        return Response(
            data
        )