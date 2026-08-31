from rest_framework import serializers

from .models import ComponentUsage


class ComponentUsageSerializer(serializers.ModelSerializer):
    issued_date = serializers.DateField(required=False, allow_null=True)
    received_date = serializers.DateField(required=False, allow_null=True)
    return_due_date = serializers.DateField(required=False, allow_null=True)

    component = serializers.PrimaryKeyRelatedField(
        queryset=__import__("components.models", fromlist=["Component"]).Component.objects.all(),
        required=False,
        allow_null=True,
    )
    component_code = serializers.CharField(source="component.component_id", read_only=True, default="")
    component_category = serializers.CharField(source="component.category", read_only=True, default="")

    material_request_number = serializers.CharField(
        source="material_request.material_request_id",
        read_only=True,
        default="",
    )
    material_request_status = serializers.CharField(
        source="material_request.status",
        read_only=True,
        default="",
    )
    material_request_approval_status = serializers.CharField(
        source="material_request.approval_status",
        read_only=True,
        default="",
    )
    request_type = serializers.CharField(
        source="material_request.request_type",
        read_only=True,
        default="",
    )

    # Flight Test / Demo / Event reuse the original In-Drone MR.
    # Expose that MR's project directly on every ComponentUsage row so the
    # Returnable page does not have to guess or cross-match another endpoint.
    project = serializers.CharField(
        source="material_request.project",
        read_only=True,
        default="",
    )
    project_name = serializers.CharField(
        source="material_request.project",
        read_only=True,
        default="",
    )

    selected_serials = serializers.ListField(
        child=serializers.CharField(),
        write_only=True,
        required=False,
        allow_empty=True,
    )
    component_name = serializers.CharField(required=False, allow_blank=True)
    component_type = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(read_only=True)
    issued_serial_numbers = serializers.ListField(child=serializers.CharField(), read_only=True)
    inventory_issue_details = serializers.JSONField(read_only=True)
    inventory_adjusted = serializers.BooleanField(read_only=True)
    inventory_returned = serializers.BooleanField(read_only=True)
    return_condition = serializers.CharField(read_only=True)
    return_reason = serializers.CharField(read_only=True)
    return_approval_status = serializers.CharField(read_only=True)

    class Meta:
        model = ComponentUsage
        fields = "__all__"
        read_only_fields = [
            "material_request",
            "return_condition",
            "return_reason",
            "return_approval_status",
        ]

    @staticmethod
    def _normalize_serials(values):
        if not isinstance(values, list):
            return []
        result = []
        seen = set()
        for value in values:
            serial = str(value or "").strip()
            if serial and serial not in seen:
                seen.add(serial)
                result.append(serial)
        return result

    def validate(self, attrs):
        instance = self.instance
        item_source = str(
            attrs.get("item_source", getattr(instance, "item_source", "INVENTORY"))
            or "INVENTORY"
        ).strip().upper()
        component = attrs.get("component", getattr(instance, "component", None))
        component_name = str(
            attrs.get("component_name", getattr(instance, "component_name", "")) or ""
        ).strip()
        component_type = str(
            attrs.get("component_type", getattr(instance, "component_type", "")) or ""
        ).strip()
        quantity = int(attrs.get("quantity", getattr(instance, "quantity", 1)) or 0)
        requested_date = attrs.get("requested_date", getattr(instance, "requested_date", None))
        issued_date = attrs.get("issued_date", getattr(instance, "issued_date", None))
        received_date = attrs.get("received_date", getattr(instance, "received_date", None))
        selected_serials = self._normalize_serials(attrs.get("selected_serials", []))
        attrs["selected_serials"] = selected_serials

        if quantity <= 0:
            raise serializers.ValidationError({"quantity": "Quantity must be greater than 0."})

        if item_source == "INVENTORY":
            if component is None:
                raise serializers.ValidationError({"component": "Select an In-Store component."})

            attrs["component_name"] = component.name
            attrs["component_type"] = component.category

            already_adjusted = bool(getattr(instance, "inventory_adjusted", False))
            is_mr_linked = bool(getattr(instance, "material_request_id", None))
            requires_serial_selection = (
                not is_mr_linked
                and (
                    instance is None
                    or (issued_date and not already_adjusted)
                )
            )

            if requires_serial_selection and len(selected_serials) != quantity:
                raise serializers.ValidationError(
                    {
                        "selected_serials": (
                            "Serial Number is mandatory for an In-Store Component issue. "
                            f"Select exactly {quantity} serial number(s)."
                        )
                    }
                )

            if instance is None and not issued_date and not is_mr_linked:
                raise serializers.ValidationError(
                    {"issued_date": "Issued Date is required for a direct In-Store Component Usage record."}
                )

        elif item_source == "OTHER":
            attrs["component"] = None
            if not component_name:
                raise serializers.ValidationError({"component_name": "Enter the item name."})
            if not component_type:
                raise serializers.ValidationError({"component_type": "Select a category."})
            attrs["selected_serials"] = []
        else:
            raise serializers.ValidationError({"item_source": "Item source must be INVENTORY or OTHER."})

        if received_date and not issued_date:
            # MR-linked Returnable rows can be return-decided by dedicated actions.
            if not getattr(instance, "material_request_id", None):
                raise serializers.ValidationError(
                    {"received_date": "Cannot receive/return an item before it is issued."}
                )

        if requested_date and issued_date and issued_date < requested_date:
            raise serializers.ValidationError({"issued_date": "Issued date cannot be before requested date."})
        if issued_date and received_date and received_date < issued_date:
            raise serializers.ValidationError({"received_date": "Received date cannot be before issued date."})

        return attrs

    def create(self, validated_data):
        selected_serials = validated_data.pop("selected_serials", [])
        instance = super().create(validated_data)
        instance._selected_serials_for_issue = selected_serials
        return instance

    def update(self, instance, validated_data):
        selected_serials = validated_data.pop("selected_serials", [])
        instance = super().update(instance, validated_data)
        instance._selected_serials_for_issue = selected_serials
        return instance
