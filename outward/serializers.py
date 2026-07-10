from django.utils import timezone
from rest_framework import serializers
from .models import OutwardEntry


type_choices = [choice[0] for choice in OutwardEntry.OUTWARD_TYPE_CHOICES]


class OutwardEntrySerializer(serializers.ModelSerializer):
    typeOfOutward = serializers.ChoiceField(
        choices=OutwardEntry.OUTWARD_TYPE_CHOICES,
        source='outward_type',
        write_only=True,
        required=False,
        allow_null=True,
    )
    type = serializers.ChoiceField(
        choices=OutwardEntry.OUTWARD_TYPE_CHOICES,
        source='outward_type',
        write_only=True,
        required=False,
        allow_null=True,
    )
    outward_type = serializers.CharField(read_only=True)
    outDate = serializers.DateField(source='out_date', write_only=True, required=False, allow_null=True)
    outDate_read = serializers.DateField(source='out_date', read_only=True)
    time = serializers.TimeField(required=False, allow_null=True)
    productName = serializers.CharField(source='product_name', write_only=True, required=False, allow_blank=True)
    productName_read = serializers.CharField(source='product_name', read_only=True)
    invoiceNumber = serializers.CharField(source='invoice_number', write_only=True, required=False, allow_blank=True)
    invoiceNumber_read = serializers.CharField(source='invoice_number', read_only=True)
    client = serializers.CharField(required=False, allow_blank=True)
    deliverables = serializers.CharField(required=False, allow_blank=True)
    gatePass = serializers.CharField(source='gate_pass', write_only=True, required=False, allow_blank=True)
    gatePass_read = serializers.CharField(source='gate_pass', read_only=True)
    eventName = serializers.CharField(source='event_name', write_only=True, required=False, allow_blank=True)
    eventName_read = serializers.CharField(source='event_name', read_only=True)
    noOfComponents = serializers.IntegerField(source='no_of_components', write_only=True, required=False, allow_null=True)
    noOfComponents_read = serializers.IntegerField(source='no_of_components', read_only=True)
    returnDate = serializers.DateField(source='return_date', write_only=True, required=False, allow_null=True)
    returnDate_read = serializers.DateField(source='return_date', read_only=True)
    droneName = serializers.CharField(source='drone_name', write_only=True, required=False, allow_blank=True, allow_null=True)
    droneName_read = serializers.CharField(source='drone_name', read_only=True)
    attendeeName = serializers.CharField(source='attendee_name', write_only=True, required=False, allow_blank=True, allow_null=True)
    attendeeName_read = serializers.CharField(source='attendee_name', read_only=True)
    eventComponents = serializers.CharField(source='event_components', write_only=True, required=False, allow_blank=True, allow_null=True)
    eventComponents_read = serializers.CharField(source='event_components', read_only=True)
    isReturned = serializers.BooleanField(source='is_returned', write_only=True, required=False)
    isReturned_read = serializers.BooleanField(source='is_returned', read_only=True)
    returned = serializers.BooleanField(source='is_returned', write_only=True, required=False)
    remarks = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False, allow_blank=True)
    code = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = OutwardEntry
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        code = validated_data.get('code')
        if not code:
            validated_data['code'] = f"OUT-{int(timezone.now().timestamp() * 1000)}"
        return super().create(validated_data)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['outDate'] = data.get('out_date')
        data['productName'] = data.get('product_name')
        data['invoiceNumber'] = data.get('invoice_number')
        data['gatePass'] = data.get('gate_pass')
        data['eventName'] = data.get('event_name')
        data['noOfComponents'] = data.get('no_of_components')
        data['returnDate'] = data.get('return_date')
        data['typeOfOutward'] = data.get('outward_type')
        data['type'] = data.get('outward_type')
        data['droneName'] = data.get('drone_name')
        data['attendeeName'] = data.get('attendee_name')
        data['eventComponents'] = data.get('event_components')
        data['isReturned'] = data.get('is_returned')
        data['returned'] = data.get('is_returned')
        return data
