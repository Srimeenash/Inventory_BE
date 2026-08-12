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

    @staticmethod
    def get_user_display_name(user):
        if (
            not user
            or not getattr(
                user,
                "is_authenticated",
                False,
            )
        ):
            return ""

        candidates = [
            getattr(
                user,
                "employee_name",
                "",
            ),
            getattr(
                user,
                "employeeName",
                "",
            ),
            getattr(
                user,
                "full_name",
                "",
            ),
            getattr(
                user,
                "name",
                "",
            ),
        ]

        try:
            candidates.append(
                user.get_full_name()
            )
        except Exception:
            pass

        candidates.extend(
            [
                getattr(
                    user,
                    "username",
                    "",
                ),
                getattr(
                    user,
                    "email",
                    "",
                ),
            ]
        )

        for value in candidates:
            raw = str(
                value or ""
            ).strip()

            if not raw:
                continue

            # User name only; never store the full email.
            if "@" in raw:
                raw = (
                    raw.split("@")[0]
                    .strip()
                )

            if raw:
                return raw[:150]

        return ""

    def perform_create(self, serializer):
        requested_by = (
            self.get_user_display_name(
                self.request.user
            )
        )

        if requested_by:
            serializer.save(
                requested_by=requested_by
            )
            return

        # Fallback to requested_by sent by the authenticated frontend.
        serializer.save()

    def get_queryset(self):
        queryset = (
            Notification.objects
            .all()
            .order_by(
                "-created_at",
                "-id",
            )
        )

        category = (
            self.request
            .query_params
            .get("category")
        )

        receiver = (
            self.request
            .query_params
            .get("receiver")
        )

        notification_status = (
            self.request
            .query_params
            .get("status")
        )

        reference_id = (
            self.request
            .query_params
            .get("reference_id")
        )

        is_read = (
            self.request
            .query_params
            .get("is_read")
        )

        if category:
            queryset = queryset.filter(
                category=str(
                    category
                )
                .strip()
                .upper()
            )

        if receiver:
            queryset = queryset.filter(
                receiver=str(
                    receiver
                )
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
                queryset = (
                    queryset.filter(
                        is_read=True
                    )
                )

            elif normalized_is_read in {
                "false",
                "0",
                "no",
            }:
                queryset = (
                    queryset.filter(
                        is_read=False
                    )
                )

        return queryset

    @action(
        detail=True,
        methods=[
            "patch",
            "post",
        ],
        url_path="mark-read",
    )
    def mark_read(
        self,
        request,
        pk=None,
    ):
        notification = (
            self.get_object()
        )

        notification.is_read = True

        notification.save(
            update_fields=[
                "is_read",
            ]
        )

        serializer = (
            self.get_serializer(
                notification
            )
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=[
            "patch",
            "post",
        ],
        url_path="mark-unread",
    )
    def mark_unread(
        self,
        request,
        pk=None,
    ):
        notification = (
            self.get_object()
        )

        notification.is_read = False

        notification.save(
            update_fields=[
                "is_read",
            ]
        )

        serializer = (
            self.get_serializer(
                notification
            )
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )