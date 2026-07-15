from django.utils import timezone
from rest_framework import serializers
from .models import OutwardEntry


class OutwardEntrySerializer(serializers.ModelSerializer):
    typeOfOutward = serializers.ChoiceField(
        choices=OutwardEntry.OUTWARD_TYPE_CHOICES,
        source="outward_type",
        write_only=True,
        required=False,
        allow_null=True,
    )

    outDate = serializers.DateField(
        source="out_date",
        write_only=True,
        required=False,
        allow_null=True,
    )

    productName = serializers.CharField(
        source="product_name",
        write_only=True,
        required=False,
        allow_blank=True,
    )

    invoiceNumber = serializers.CharField(
        source="invoice_number",
        write_only=True,
        required=False,
        allow_blank=True,
    )

    gatePass = serializers.CharField(
        source="gate_pass",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    eventName = serializers.CharField(
        source="event_name",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    noOfComponents = serializers.IntegerField(
        source="no_of_components",
        write_only=True,
        required=False,
        allow_null=True,
    )

    returnDate = serializers.DateField(
        source="return_date",
        write_only=True,
        required=False,
        allow_null=True,
    )

    droneName = serializers.CharField(
        source="drone_name",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    attendeeName = serializers.CharField(
        source="attendee_name",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    eventComponents = serializers.CharField(
        source="event_components",
        write_only=True,
        required=False,
        allow_blank=True,
        allow_null=True,
    )

    isReturned = serializers.BooleanField(
        source="is_returned",
        write_only=True,
        required=False,
    )
    code = serializers.CharField(
    required=False,
    read_only=True,
)

    class Meta:
        model = OutwardEntry
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]

    def create(self, validated_data):
        if not validated_data.get("code"):
            validated_data["code"] = f"OUT-{int(timezone.now().timestamp() * 1000)}"
        if not validated_data.get("status"):
            validated_data["status"] = "NEW"
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # Handle approval status updates
        approval_status = validated_data.get('approval_status')
        if approval_status:
            # Sync status with approval_status
            if approval_status == 'APPROVED':
                validated_data['status'] = 'APPROVED'
            elif approval_status == 'REJECTED':
                validated_data['status'] = 'REJECTED'
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)

        data["outDate"] = data["out_date"]
        data["productName"] = data["product_name"]
        data["invoiceNumber"] = data["invoice_number"]
        data["gatePass"] = data["gate_pass"]
        data["eventName"] = data["event_name"]
        data["noOfComponents"] = data["no_of_components"]
        data["returnDate"] = data["return_date"]
        data["droneName"] = data["drone_name"]
        data["attendeeName"] = data["attendee_name"]
        data["eventComponents"] = data["event_components"]
        data["isReturned"] = data["is_returned"]
        data["typeOfOutward"] = data["outward_type"]

        return data