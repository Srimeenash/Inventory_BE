from django.db.models import Q

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .models import Notification
from .serializers import NotificationSerializer


class NotificationViewSet(viewsets.ModelViewSet):
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
            getattr(user, "employee_name", ""),
            getattr(user, "employeeName", ""),
            getattr(user, "full_name", ""),
            getattr(user, "name", ""),
        ]

        try:
            candidates.append(user.get_full_name())
        except Exception:
            pass

        candidates.extend(
            [
                getattr(user, "username", ""),
                getattr(user, "email", ""),
            ]
        )

        for value in candidates:
            raw = str(value or "").strip()

            if not raw:
                continue

            # User name only; never store the full email.
            if "@" in raw:
                raw = raw.split("@")[0].strip()

            if raw:
                return raw[:150]

        return ""

    @staticmethod
    def normalize_upper(value):
        return str(value or "").strip().upper()

    def perform_create(self, serializer):
        """
        Keep normal notification creation compatible with the current app,
        while preventing old frontend code from creating a Manager Scrap
        notification before Finance approval.

        Scrap workflow authority lives in outward/views.py:
            Scrap create       -> FINANCE notification
            Finance approve    -> MANAGER notification
            Manager decision   -> exact creator notification
        """
        category = self.normalize_upper(
            serializer.validated_data.get("category")
        )
        receiver = self.normalize_upper(
            serializer.validated_data.get("receiver")
        )
        notification_status = self.normalize_upper(
            serializer.validated_data.get("status")
        )
        reference_id = str(
            serializer.validated_data.get("reference_id")
            or ""
        ).strip()

        # HARD GUARD AGAINST OLD FRONTEND MANAGER-FIRST SCRAP CREATION.
        if category == "SCRAP" and receiver == "MANAGER":
            if notification_status != "PENDING_MANAGER":
                raise ValidationError(
                    {
                        "detail": (
                            "Manager Scrap notifications must use "
                            "PENDING_MANAGER."
                        )
                    }
                )

            if not reference_id:
                raise ValidationError(
                    {
                        "reference_id": (
                            "Scrap reference_id is required."
                        )
                    }
                )

            # Local import avoids a module-level circular dependency because
            # outward/views.py also imports Notification.
            from outward.models import OutwardEntry

            instance = (
                OutwardEntry.objects
                .filter(pk=reference_id)
                .only(
                    "id",
                    "outward_type",
                    "approval_status",
                    "status",
                )
                .first()
            )

            if not instance:
                raise ValidationError(
                    {
                        "detail": (
                            "The referenced Scrap record was not found."
                        )
                    }
                )

            if self.normalize_upper(
                instance.outward_type
            ) != "SCRAP":
                raise ValidationError(
                    {
                        "detail": (
                            "Manager Scrap notifications can reference "
                            "only Scrap Outward records."
                        )
                    }
                )

            current = self.normalize_upper(
                instance.approval_status
                or instance.status
            )

            if current != "PENDING_MANAGER":
                raise ValidationError(
                    {
                        "detail": (
                            "Manager Scrap notification is blocked until "
                            "Finance approves the Scrap. "
                            f"Current state: {current or 'UNKNOWN'}."
                        )
                    }
                )

        requested_by = self.get_user_display_name(
            self.request.user
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
            .select_related("recipient_user")
            .all()
            .order_by(
                "-created_at",
                "-id",
            )
        )

        user = getattr(
            self.request,
            "user",
            None,
        )

        # Exact-user notifications are private. Existing role-level
        # notifications remain compatible with the current frontend.
        if (
            user
            and getattr(
                user,
                "is_authenticated",
                False,
            )
        ):
            queryset = queryset.filter(
                Q(recipient_user__isnull=True)
                | Q(recipient_user=user)
            )
        else:
            queryset = queryset.filter(
                recipient_user__isnull=True
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

        # Use ?recipient=me for the final Scrap result notification that
        # belongs only to the logged-in creator.
        recipient = (
            self.request
            .query_params
            .get("recipient")
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

        if recipient:
            normalized_recipient = str(
                recipient
            ).strip().lower()

            if normalized_recipient == "me":
                if (
                    user
                    and getattr(
                        user,
                        "is_authenticated",
                        False,
                    )
                ):
                    queryset = queryset.filter(
                        recipient_user=user
                    )
                else:
                    queryset = queryset.none()

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
        notification = self.get_object()
        notification.is_read = True
        notification.save(
            update_fields=[
                "is_read",
            ]
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
        notification = self.get_object()
        notification.is_read = False
        notification.save(
            update_fields=[
                "is_read",
            ]
        )

        serializer = self.get_serializer(
            notification
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )