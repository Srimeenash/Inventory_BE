"""
Temporary API SQL/query-count performance logger.

Enable with:
    API_PERFORMANCE_LOGGING=True

This middleware is read-only:
- it does not modify responses;
- it does not alter transactions;
- it does not change workflow/status/approval behavior;
- it does not require migrations or third-party packages.
"""

from collections import Counter
import logging
import re
import time

from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext


logger = logging.getLogger(
    "api.performance"
)


_STRING_LITERAL_RE = re.compile(
    r"'(?:''|\\'|[^'])*'"
)

_NUMBER_LITERAL_RE = re.compile(
    r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])"
)

_WHITESPACE_RE = re.compile(
    r"\s+"
)


def _normalize_sql(sql):
    """
    Normalize SQL values so structurally identical N+1 queries such as:

        WHERE id = 1
        WHERE id = 2
        WHERE id = 3

    are counted as duplicates of the same query pattern.
    """
    value = str(sql or "")

    value = _STRING_LITERAL_RE.sub(
        "?",
        value,
    )

    value = _NUMBER_LITERAL_RE.sub(
        "?",
        value,
    )

    return _WHITESPACE_RE.sub(
        " ",
        value,
    ).strip()


def _query_seconds(query):
    try:
        return float(
            query.get(
                "time",
                0,
            )
            or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        return 0.0


def _compact_sql(sql, limit=500):
    value = _WHITESPACE_RE.sub(
        " ",
        str(sql or ""),
    ).strip()

    if len(value) <= limit:
        return value

    return (
        value[:limit]
        + " ..."
    )


class ApiPerformanceLoggingMiddleware:
    """
    Measure SQL and wall-clock time per request.

    The middleware only exists in MIDDLEWARE when
    API_PERFORMANCE_LOGGING=True.
    """

    SKIP_PREFIXES = (
        "/static/",
        "/media/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = str(
            getattr(
                request,
                "path",
                "",
            )
            or ""
        )

        if path.startswith(
            self.SKIP_PREFIXES
        ):
            return self.get_response(
                request
            )

        started_at = (
            time.perf_counter()
        )

        with CaptureQueriesContext(
            connection
        ) as captured:
            response = (
                self.get_response(
                    request
                )
            )

        elapsed_seconds = (
            time.perf_counter()
            - started_at
        )

        queries = list(
            captured.captured_queries
        )

        query_count = len(
            queries
        )

        sql_seconds = sum(
            _query_seconds(query)
            for query in queries
        )

        normalized_counter = Counter(
            _normalize_sql(
                query.get(
                    "sql",
                    "",
                )
            )
            for query in queries
            if query.get(
                "sql"
            )
        )

        duplicate_groups = [
            (
                sql,
                count,
            )
            for sql, count
            in normalized_counter.items()
            if count > 1
        ]

        duplicate_groups.sort(
            key=lambda row: row[1],
            reverse=True,
        )

        duplicate_query_excess = sum(
            count - 1
            for _, count
            in duplicate_groups
        )

        slow_threshold = float(
            getattr(
                settings,
                "API_PERFORMANCE_SLOW_QUERY_SECONDS",
                0.10,
            )
            or 0.10
        )

        slow_queries = [
            query
            for query in queries
            if (
                _query_seconds(
                    query
                )
                >= slow_threshold
            )
        ]

        slow_queries.sort(
            key=_query_seconds,
            reverse=True,
        )

        method = str(
            getattr(
                request,
                "method",
                "",
            )
            or ""
        )

        status_code = getattr(
            response,
            "status_code",
            "?",
        )

        logger.info(
            (
                "[API PERF] %s %s | "
                "status=%s | "
                "total=%.3fs | "
                "sql=%.3fs | "
                "queries=%d | "
                "duplicate_excess=%d | "
                "slow_queries=%d"
            ),
            method,
            path,
            status_code,
            elapsed_seconds,
            sql_seconds,
            query_count,
            duplicate_query_excess,
            len(slow_queries),
        )

        top_duplicates = int(
            getattr(
                settings,
                "API_PERFORMANCE_TOP_DUPLICATES",
                5,
            )
            or 5
        )

        for index, (
            normalized_sql,
            count,
        ) in enumerate(
            duplicate_groups[
                :top_duplicates
            ],
            start=1,
        ):
            logger.info(
                (
                    "[API PERF DUP %d] "
                    "count=%d | %s"
                ),
                index,
                count,
                _compact_sql(
                    normalized_sql
                ),
            )

        top_slow = int(
            getattr(
                settings,
                "API_PERFORMANCE_TOP_SLOW_QUERIES",
                5,
            )
            or 5
        )

        for index, query in enumerate(
            slow_queries[
                :top_slow
            ],
            start=1,
        ):
            logger.info(
                (
                    "[API PERF SLOW %d] "
                    "%.3fs | %s"
                ),
                index,
                _query_seconds(
                    query
                ),
                _compact_sql(
                    query.get(
                        "sql",
                        "",
                    )
                ),
            )

        return response
