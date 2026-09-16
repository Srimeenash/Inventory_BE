from django.core.cache import cache
from rest_framework import viewsets
from rest_framework.response import Response
from inventory_backend.cache_utils import (
    build_list_cache_key,
    get_cache_version,
    invalidate_cache_version,
)
from .models import Invoice, Payment, FinanceLedger
from .serializers import InvoiceSerializer, PaymentSerializer, FinanceLedgerSerializer

FINANCE_LIST_CACHE_TTL_SECONDS = 60
FINANCE_LIST_CACHE_VERSION_KEY = "ipms:finance:list:version"


def get_finance_cache_version():
    return get_cache_version(FINANCE_LIST_CACHE_VERSION_KEY)


def invalidate_finance_cache():
    invalidate_cache_version(FINANCE_LIST_CACHE_VERSION_KEY)


class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = Invoice.objects.all().order_by('-created_at')
    serializer_class = InvoiceSerializer

    def list(self, request, *args, **kwargs):
        version = get_finance_cache_version()
        cache_key = None

        if version:
            cache_key = build_list_cache_key(
                "ipms:finance:invoices:list",
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
                    timeout=FINANCE_LIST_CACHE_TTL_SECONDS,
                )
            except Exception:
                pass

        return response


class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.all().order_by('-paid_date')
    serializer_class = PaymentSerializer

    def list(self, request, *args, **kwargs):
        version = get_finance_cache_version()
        cache_key = None

        if version:
            cache_key = build_list_cache_key(
                "ipms:finance:payments:list",
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
                    timeout=FINANCE_LIST_CACHE_TTL_SECONDS,
                )
            except Exception:
                pass

        return response


class FinanceLedgerViewSet(viewsets.ModelViewSet):
    queryset = FinanceLedger.objects.all().order_by('-created_at')
    serializer_class = FinanceLedgerSerializer

    def list(self, request, *args, **kwargs):
        version = get_finance_cache_version()
        cache_key = None

        if version:
            cache_key = build_list_cache_key(
                "ipms:finance:ledger:list",
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
                    timeout=FINANCE_LIST_CACHE_TTL_SECONDS,
                )
            except Exception:
                pass

        return response
