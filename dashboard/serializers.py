from rest_framework import serializers


class DashboardSerializer(serializers.Serializer):
    total_components = serializers.IntegerField()
    total_stock_value = serializers.DecimalField(max_digits=20, decimal_places=2)

    low_stock_items = serializers.IntegerField()

    pending_pr = serializers.IntegerField()
    approved_pr = serializers.IntegerField()

    pending_po = serializers.IntegerField()

    pending_approvals = serializers.IntegerField()

    total_bom = serializers.IntegerField()
    active_bom = serializers.IntegerField()