from rest_framework import serializers

from .models import Notification


class NotificationSerializer(
    serializers.ModelSerializer
):
    class Meta:
        model = Notification
        fields = "__all__"

        read_only_fields = [
            "id",
            "created_at",
        ]

    def validate_category(self, value):
        return str(value or "").strip().upper()

    def validate_status(self, value):
        return str(value or "").strip().upper()

    def validate_receiver(self, value):
        if value in (None, ""):
            return None

        return str(value).strip().upper()

    def validate_reference_id(self, value):
        if value in (None, ""):
            return None

        return str(value).strip()