from rest_framework import serializers
from .models import InwardEntry, InwardLineItem
from components.models import Component
from procurement.models import PurchaseOrder


class FlexibleComponentRelatedField(serializers.PrimaryKeyRelatedField):
    def to_internal_value(self, data):
        if data is None or data == '':
            self.fail('required')

        if isinstance(data, str):
            value = data.strip()
            if '—' in value:
                value = value.split('—', 1)[0].strip()
            elif ':' in value:
                value = value.split(':', 1)[0].strip()
            elif '-' in value and not value.startswith('CMP-'):
                value = value.split('-', 1)[0].strip()

            if value.isdigit():
                try:
                    return Component.objects.get(pk=int(value))
                except Component.DoesNotExist:
                    pass

            try:
                return Component.objects.get(component_id=value)
            except Component.DoesNotExist:
                pass

            try:
                return Component.objects.get(name=value)
            except Component.DoesNotExist:
                pass

        try:
            return super().to_internal_value(data)
        except serializers.ValidationError:
            raise serializers.ValidationError('Invalid component reference.')


class FlexiblePurchaseOrderRelatedField(serializers.PrimaryKeyRelatedField):
    def to_internal_value(self, data):
        if data is None or data == '':
            return None

        if isinstance(data, str):
            value = data.strip()
            if '—' in value:
                value = value.split('—', 1)[0].strip()
            elif ':' in value:
                value = value.split(':', 1)[0].strip()
            elif '-' in value and not value.startswith('PO-'):
                value = value.split('-', 1)[0].strip()

            if value.isdigit():
                try:
                    return PurchaseOrder.objects.get(pk=int(value))
                except PurchaseOrder.DoesNotExist:
                    pass

            try:
                return PurchaseOrder.objects.get(po_number=value)
            except PurchaseOrder.DoesNotExist:
                pass

        try:
            return super().to_internal_value(data)
        except serializers.ValidationError:
            raise serializers.ValidationError('Invalid purchase order reference.')


class InwardLineItemSerializer(serializers.ModelSerializer):
    inward_entry = serializers.PrimaryKeyRelatedField(read_only=True)
    invoiceNo = serializers.CharField(source='invoice_number', write_only=True, required=False, allow_blank=True)
    invoiceDate = serializers.DateField(source='invoice_date', write_only=True, required=False, allow_null=True)
    totalQty = serializers.IntegerField(source='total_quantity', write_only=True, required=False, allow_null=True)
    gst = serializers.DecimalField(source='gst_percentage', max_digits=5, decimal_places=2, write_only=True, required=False, allow_null=True)
    grandTotal = serializers.DecimalField(source='grand_total', max_digits=18, decimal_places=2, write_only=True, required=False, allow_null=True)

    class Meta:
        model = InwardLineItem
        fields = [
            'id',
            'inward_entry',
            'specification',
            'invoice_number',
            'invoice_date',
            'total_quantity',
            'quantity',
            'unit_price',
            'gst_percentage',
            'grand_total',
            'invoiceNo',
            'invoiceDate',
            'totalQty',
            'gst',
            'grandTotal',
        ]
        read_only_fields = ['id', 'inward_entry']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['invoiceNo'] = data.get('invoice_number')
        data['invoiceDate'] = data.get('invoice_date')
        data['totalQty'] = data.get('total_quantity')
        data['gst'] = data.get('gst_percentage')
        data['grandTotal'] = data.get('grand_total')
        return data


class InwardEntrySerializer(serializers.ModelSerializer):
    component = FlexibleComponentRelatedField(queryset=Component.objects.all())
    purchase_order = FlexiblePurchaseOrderRelatedField(
        queryset=PurchaseOrder.objects.all(),
        required=False,
        allow_null=True,
    )
    po = FlexiblePurchaseOrderRelatedField(
        queryset=PurchaseOrder.objects.all(),
        source='purchase_order',
        write_only=True,
        required=False,
        allow_null=True,
    )
    date = serializers.DateField(source='received_date', write_only=True, required=False)
    receivedDate = serializers.DateField(source='received_date', write_only=True, required=False)
    batchNumber = serializers.CharField(source='batch_number', write_only=True, required=False, allow_blank=True)
    quantity = serializers.IntegerField(source='quantity_received', write_only=True, required=False)
    items = serializers.IntegerField(source='quantity_received', write_only=True, required=False)
    qc = serializers.CharField(source='qc_status', write_only=True, required=False, allow_blank=True)
    passedRows = serializers.JSONField(source='qc_passed_rows', read_only=True)
    failedRows = serializers.JSONField(source='qc_failed_rows', read_only=True)
    qcTimestamp = serializers.DateTimeField(source='qc_timestamp', read_only=True)
    line_items = InwardLineItemSerializer(many=True, required=False)

    class Meta:
        model = InwardEntry
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        line_items_data = validated_data.pop('line_items', [])
        inward_entry = InwardEntry.objects.create(**validated_data)
        for item_data in line_items_data:
            item_data.pop('inward_entry', None)
            InwardLineItem.objects.create(inward_entry=inward_entry, **item_data)
        return inward_entry

    def update(self, instance, validated_data):
        line_items_data = validated_data.pop('line_items', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if line_items_data is not None:
            instance.line_items.all().delete()
            for item_data in line_items_data:
                item_data.pop('inward_entry', None)
                InwardLineItem.objects.create(inward_entry=instance, **item_data)
        return instance
