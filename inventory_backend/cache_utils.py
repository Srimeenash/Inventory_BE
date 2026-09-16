from hashlib import sha256
from urllib.parse import urlencode
from uuid import uuid4

from django.core.cache import cache


def build_query_hash(request):
    """
    Build a deterministic query string hash for request parameters.

    This keeps cache entries unique for each combination of filters,
    search, ordering, and pagination parameters without needing to
    delete Redis keys by wildcard.
    """
    pairs = []

    for key in sorted(request.query_params.keys()):
        values = request.query_params.getlist(key)

        if not values:
            pairs.append((key, ""))
            continue

        for value in values:
            pairs.append((str(key), str(value)))

    query_string = urlencode(pairs, doseq=True)
    return sha256(query_string.encode("utf-8")).hexdigest()


def build_list_cache_key(prefix, version, request):
    """Create a cache key for paginated list endpoints."""
    query_hash = build_query_hash(request)
    return f"{prefix}:{version}:{query_hash}"


def get_cache_version(version_key):
    """Return the current cache version or create one when missing."""
    try:
        version = cache.get(version_key)
        if version is None:
            version = uuid4().hex
            cache.set(version_key, version, timeout=None)
        return str(version)
    except Exception:
        return None


def invalidate_cache_version(version_key):
    """Bump the namespace version to invalidate all cached list entries."""
    try:
        cache.set(version_key, uuid4().hex, timeout=None)
    except Exception:
        pass
