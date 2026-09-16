from rest_framework.pagination import PageNumberPagination
from django.db.models import Q


class OptionalPageNumberPagination(PageNumberPagination):
    """
    Backward-compatible server-side pagination.

    Existing IPMS pages keep the current plain-array response unless they
    explicitly opt in with:

        ?paginate=1&page_size=50

    New/optimized pages should use paginate=1.

    Protection:
    - default page size: 50
    - client may request page_size
    - hard maximum: 100

    This lets the frontend migrate page-by-page without breaking older code.
    """

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100
    page_query_param = "page"

    ENABLE_VALUES = {
        "1",
        "true",
        "yes",
        "on",
    }

    def paginate_queryset(
        self,
        queryset,
        request,
        view=None,
    ):
        enabled = str(
            request.query_params.get(
                "paginate",
                "",
            )
            or ""
        ).strip().lower()

        if enabled not in self.ENABLE_VALUES:
            return None

        return super().paginate_queryset(
            queryset,
            request,
            view=view,
        )


def apply_server_query_parameters(
    queryset,
    request,
    *,
    search_fields=(),
    filter_fields=(),
    boolean_fields=(),
    ordering_fields=(),
    default_ordering=(),
):
    """Apply allowlisted search, filter_*, and ordering parameters."""
    search_value = str(
        request.query_params.get("search", "") or ""
    ).strip()

    if search_value and search_fields:
        search_query = Q()
        for field_name in search_fields:
            search_query |= Q(
                **{f"{field_name}__icontains": search_value}
            )
        queryset = queryset.filter(search_query)

    filter_kwargs = {}
    for frontend_name, django_lookup in dict(filter_fields).items():
        value = str(
            request.query_params.get(
                f"filter_{frontend_name}",
                "",
            )
            or ""
        ).strip()
        if value:
            if frontend_name in set(boolean_fields):
                normalized_value = value.lower()
                if normalized_value in {"true", "1", "yes", "on"}:
                    value = True
                elif normalized_value in {"false", "0", "no", "off"}:
                    value = False
            filter_kwargs[django_lookup] = value

    if filter_kwargs:
        queryset = queryset.filter(**filter_kwargs)

    ordering_value = str(
        request.query_params.get("ordering", "") or ""
    ).strip()
    allowed_ordering = set(ordering_fields)

    if ordering_value:
        field_name = ordering_value.lstrip("-")
        if field_name in allowed_ordering:
            return queryset.order_by(ordering_value, "-id")

    if default_ordering:
        return queryset.order_by(*default_ordering)

    return queryset
