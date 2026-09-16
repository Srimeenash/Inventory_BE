from rest_framework import serializers
from django.db import transaction
from django.db.utils import IntegrityError

from .models import Component


CATEGORY_PREFIXES = {
    "ACCESSORIES": "AC",
    "AIRFRAMES": "AF",
    "COMMUNICATION": "CM",
    "ELECTRICALS": "EL",
    "ELECTRONICS": "EN",
    "PAYLOAD": "PL",
    "TOOLS": "TL",
}


class ComponentSerializer(
    serializers.ModelSerializer
):
    component_id = serializers.CharField(
        required=False,
        read_only=True,
    )

    class Meta:
        model = Component
        fields = [
            "id",
            "component_id",
            "category",
            "component_type",
            "specifications",
            "unit_of_measurements",
            "hsn_numbers",
            "version",
            "sku_numbers",
            "part_numbers",
            "product_link",
            "ordering_id",
            "unit_price",
            "tally_reference",
            "stock_quantity",
            "reorder_level",
            "total_value",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "component_id",
            "total_value",
            "created_at",
            "updated_at",
        ]

    def validate_hsn_numbers(self, value):
        value = str(value or "").strip()

        if not value:
            return ""

        if not value.isdigit() or not 4 <= len(value) <= 8:
            raise serializers.ValidationError(
                "HSN number must contain 4 to 8 digits."
            )

        return value

    def to_representation(self, instance):
        representation = (
            super().to_representation(
                instance
            )
        )

        request = self.context.get(
            "request"
        )
        user = getattr(
            request,
            "user",
            None,
        )
        role = getattr(
            user,
            "role",
            None,
        )
        role_name = getattr(
            role,
            "name",
            None,
        )

        if role_name in [
            "ENGINEER",
            "ENGINEERING_MANAGER",
        ]:
            representation.pop(
                "unit_price",
                None,
            )
            representation.pop(
                "total_value",
                None,
            )

        return representation

    def generate_component_id(
        self,
        category,
    ):
        category = str(
            category or ""
        ).strip().upper()

        prefix = CATEGORY_PREFIXES.get(
            category
        )

        if not prefix:
            raise serializers.ValidationError(
                {
                    "category":
                        "Invalid component category."
                }
            )

        existing_ids = Component.objects.values_list(
            "component_id",
            flat=True,
        )

        highest_number = 0

        for component_id in existing_ids:
            value = str(
                component_id or ""
            ).strip()

            if "_" not in value:
                continue

            number_part = value.rsplit("_", 1)[-1]

            if (
                len(number_part) != 4
                or not number_part.isdigit()
            ):
                continue

            highest_number = max(
                highest_number,
                int(number_part),
            )

        return (
            f"{prefix}_"
            f"{highest_number + 1:04d}"
        )

    @transaction.atomic
    def create(self, validated_data):
        category = str(
            validated_data.get(
                "category"
            )
            or ""
        ).strip().upper()

        if not category:
            raise serializers.ValidationError(
                {
                    "category":
                        "Category is required."
                }
            )

        validated_data["category"] = (
            category
        )
        validated_data.pop(
            "component_id",
            None,
        )

        for _ in range(5):
            component_id = (
                self.generate_component_id(
                    category
                )
            )

            try:
                return Component.objects.create(
                    component_id=
                        component_id,
                    **validated_data,
                )
            except IntegrityError:
                continue

        raise serializers.ValidationError(
            {
                "component_id": (
                    "Unable to generate a unique "
                    "Component ID. Please try again."
                )
            }
        )

    def update(
        self,
        instance,
        validated_data,
    ):
        validated_data.pop(
            "component_id",
            None,
        )

        if "category" in validated_data:
            validated_data[
                "category"
            ] = str(
                validated_data["category"]
            ).strip().upper()

        return super().update(
            instance,
            validated_data,
        )


class ComponentLookupSerializer(
    serializers.ModelSerializer
):
    class Meta:
        model = Component
        fields = [
            "id",
            "component_id",
            "category",
            "component_type",
            "unit_of_measurements",
            "is_active",
        ]
