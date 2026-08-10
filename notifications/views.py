from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Notification
from .serializers import NotificationSerializer


class NotificationViewSet(
    viewsets.ModelViewSet
):
    serializer_class = NotificationSerializer
    pagination_class = None

    def get_queryset(self):
        queryset = (
            Notification.objects
            .all()
            .order_by("-created_at", "-id")
        )

        category = self.request.query_params.get(
            "category"
        )

        receiver = self.request.query_params.get(
            "receiver"
        )

        notification_status = (
            self.request.query_params.get(
                "status"
            )
        )

        reference_id = (
            self.request.query_params.get(
                "reference_id"
            )
        )

        is_read = self.request.query_params.get(
            "is_read"
        )

        if category:
            queryset = queryset.filter(
                category=str(category)
                .strip()
                .upper()
            )

        if receiver:
            queryset = queryset.filter(
                receiver=str(receiver)
                .strip()
                .upper()
            )

        if notification_status:
            statuses = [
                value.strip().upper()
                for value in str(
                    notification_status
                ).split(",")
                if value.strip()
            ]

            if statuses:
                queryset = queryset.filter(
                    status__in=statuses
                )

        if reference_id:
            queryset = queryset.filter(
                reference_id=str(
                    reference_id
                ).strip()
            )

        if is_read is not None:
            normalized_is_read = str(
                is_read
            ).strip().lower()

            if normalized_is_read in {
                "true",
                "1",
                "yes",
            }:
                queryset = queryset.filter(
                    is_read=True
                )

            elif normalized_is_read in {
                "false",
                "0",
                "no",
            }:
                queryset = queryset.filter(
                    is_read=False
                )

        return queryset

    @action(
        detail=True,
        methods=["patch", "post"],
        url_path="mark-read",
    )
    def mark_read(self, request, pk=None):
        notification = self.get_object()

        notification.is_read = True
        notification.save(
            update_fields=["is_read"]
        )

        serializer = self.get_serializer(
            notification
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["patch", "post"],
        url_path="mark-unread",
    )
    def mark_unread(self, request, pk=None):
        notification = self.get_object()

        notification.is_read = False
        notification.save(
            update_fields=["is_read"]
        )

        serializer = self.get_serializer(
            notification
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )