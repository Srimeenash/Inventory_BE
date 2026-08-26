from uuid import uuid4
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from inventory.models import (
    Inventory,
    InventoryReservation,
    ProjectInventory,
)
from materialrequest.models import MaterialRequest, BOMItem, RDItem
from notifications.email_service import send_ipms_email
from notifications.models import Notification

from .models import OutwardEntry
from .serializers import OutwardEntrySerializer


User = get_user_model()


class OutwardEntryViewSet(viewsets.ModelViewSet):
    """
    Direct Sales/Event Outward workflow.

    SALES + COMPONENT:
        Deducts central In-Store quantity and exact serials permanently.

    EVENT + COMPONENT:
        Deducts central In-Store quantity and exact serials temporarily.
        Returned-good quantity is restored through PATCH.

    SALES/EVENT + DRONE:
        Stores only the manually entered drone name. It has no MR link and
        does not change component Inventory.
    """

    queryset = (
        OutwardEntry.objects
        .select_related(
            "component",
            "material_request",
        )
        .all()
        .order_by("-out_date", "-created_at", "-id")
    )
    serializer_class = OutwardEntrySerializer
    pagination_class = None

    # Parse `Authorization: Bearer <access-token>` for this ViewSet.
    #
    # This is important for manager-approve / manager-reject because
    # those actions use request.user to verify that the caller is
    # actually a Manager.
    authentication_classes = [
        JWTAuthentication,
    ]

    def get_queryset(self):
        """
        Normal Inventory/Outward screens call GET /outward/.

        Engineer-raised Scrap must NOT appear there until the Engineer
        explicitly clicks "Move to Inventory" after Manager approval.

        Detail/custom actions still see the staged row so Manager
        Notifications can approve/reject it by ID.
        """
        queryset = super().get_queryset()

        if getattr(self, "action", "") == "list":
            return queryset.filter(
                Q(source="DIRECT")
                | Q(
                    source="ENGINEER",
                    moved_to_inventory=True,
                )
            )

        return queryset

    @staticmethod
    def normalize_role_value(value):
        """
        Normalize one role value coming from the User model.

        The project has used role values through more than one API/model
        attribute (role, client_type, user_type). Related role objects are
        also supported through common name/code/slug/value attributes.
        """
        if value is None:
            return ""

        for attribute in ("name", "code", "slug", "value"):
            if hasattr(value, attribute):
                nested = getattr(value, attribute, None)
                if nested not in (None, ""):
                    value = nested
                    break

        return str(value or "").strip().lower()

    @classmethod
    def get_user_role_candidates(cls, user):
        """
        Return every non-empty role representation stored on the user.

        This is intentionally fail-closed. If an account says FINANCE in
        one field and MANAGER in another, it must not be selected for either
        Scrap email stage until the user data is corrected.
        """
        if not user:
            return set()

        roles = set()

        for attribute in ("role", "client_type", "user_type"):
            if not hasattr(user, attribute):
                continue

            normalized = cls.normalize_role_value(
                getattr(user, attribute, None)
            )

            if normalized:
                roles.add(normalized)

        return roles

    @classmethod
    def user_has_exact_role(cls, user, expected_role):
        """
        True only when every available role field agrees on one role.

        Examples:
            {"finance"}            -> Finance allowed
            {"manager"}            -> Manager allowed
            {"finance", "manager"} -> blocked from both
        """
        expected = cls.normalize_role_value(expected_role)
        roles = cls.get_user_role_candidates(user)
        return bool(expected) and roles == {expected}

    @classmethod
    def get_user_role(cls, user):
        """
        Return the user's role only when all stored role fields agree.
        """
        roles = cls.get_user_role_candidates(user)

        if len(roles) != 1:
            return ""

        return next(iter(roles))

    @classmethod
    def require_manager(cls, request):
        user = getattr(request, "user", None)

        if (
            not user
            or not user.is_authenticated
        ):
            raise PermissionDenied(
                "Authentication is required."
            )

        if cls.get_user_role(user) != "manager":
            raise PermissionDenied(
                "Only Manager can approve or reject Scrap."
            )

        return user

    @classmethod
    def require_finance(cls, request):
        user = getattr(request, "user", None)

        if (
            not user
            or not user.is_authenticated
        ):
            raise PermissionDenied(
                "Authentication is required."
            )

        if cls.get_user_role(user) != "finance":
            raise PermissionDenied(
                "Only Finance can approve or reject Scrap at this stage."
            )

        return user

    @classmethod
    def require_engineer_or_admin(
        cls,
        request,
    ):
        user = getattr(
            request,
            "user",
            None,
        )

        if (
            not user
            or not user.is_authenticated
        ):
            raise PermissionDenied(
                "Authentication is required."
            )

        role = cls.get_user_role(user)

        if not (
            role in {
                "engineer",
                "admin",
            }
            or getattr(
                user,
                "is_superuser",
                False,
            )
        ):
            raise PermissionDenied(
                "Only Engineer or Admin can manage Engineer Scrap."
            )

        return user

    @classmethod
    def is_admin_user(
        cls,
        user,
    ):
        return (
            cls.get_user_role(user)
            == "admin"
            or getattr(
                user,
                "is_superuser",
                False,
            )
        )

    @staticmethod
    def get_ipms_base_url():
        return str(
            getattr(
                settings,
                "IPMS_BASE_URL",
                "http://localhost:5173",
            )
            or "http://localhost:5173"
        ).rstrip("/")

    @staticmethod
    def get_mail_user_name(
        user,
        fallback="User",
    ):
        if not user:
            return fallback

        return (
            getattr(
                user,
                "employee_name",
                "",
            )
            or getattr(
                user,
                "name",
                "",
            )
            or getattr(
                user,
                "username",
                "",
            )
            or getattr(
                user,
                "email",
                "",
            )
            or fallback
        )

    @classmethod
    def get_scrap_reference(
        cls,
        instance,
    ):
        return (
            str(
                getattr(
                    instance,
                    "code",
                    "",
                )
                or ""
            ).strip()
            or f"SCRAP-{instance.pk}"
        )

    @classmethod
    def get_scrap_component_label(
        cls,
        instance,
    ):
        component = getattr(
            instance,
            "component",
            None,
        )

        component_code = str(
            getattr(
                component,
                "component_id",
                "",
            )
            or ""
        ).strip()

        component_name = str(
            getattr(
                component,
                "name",
                "",
            )
            or ""
        ).strip()

        return (
            " - ".join(
                value
                for value in [
                    component_code,
                    component_name,
                ]
                if value
            )
            or str(
                getattr(
                    instance,
                    "product_name",
                    "",
                )
                or ""
            ).strip()
            or "-"
        )

    @classmethod
    def resolve_scrap_requester_user(
        cls,
        instance,
    ):
        """
        Exact return-mail recipient resolution.

        New Scrap records store requested_by_user_id from request.user.
        Engineer Scrap already stores this field.

        Safe fallbacks are used only for older rows.
        No random user is ever selected.
        """
        requester_user_id = getattr(
            instance,
            "requested_by_user_id",
            None,
        )

        if requester_user_id:
            user = (
                User.objects
                .filter(
                    pk=requester_user_id,
                    is_active=True,
                )
                .first()
            )

            if user:
                return user

        # MR-linked Engineer Scrap compatibility fallback.
        material_request = getattr(
            instance,
            "material_request",
            None,
        )

        mr_requester = getattr(
            material_request,
            "requester",
            None,
        )

        if (
            mr_requester
            and getattr(
                mr_requester,
                "is_active",
                True,
            )
        ):
            return mr_requester

        requester_reference = str(
            getattr(
                instance,
                "requested_by",
                "",
            )
            or ""
        ).strip()

        if not requester_reference:
            return None

        # Exact email fallback.
        user = (
            User.objects
            .filter(
                email__iexact=requester_reference,
                is_active=True,
            )
            .first()
        )

        if user:
            return user

        # Employee-name fallback for old rows.
        try:
            user = (
                User.objects
                .filter(
                    employee_name__iexact=(
                        requester_reference
                    ),
                    is_active=True,
                )
                .first()
            )

            if user:
                return user
        except Exception:
            pass

        # Username compatibility fallback.
        try:
            user = (
                User.objects
                .filter(
                    username__iexact=(
                        requester_reference
                    ),
                    is_active=True,
                )
                .first()
            )

            if user:
                return user
        except Exception:
            pass

        return None

    @classmethod
    def ensure_scrap_finance_notification(
        cls,
        instance,
        requester_name,
    ):
        """
        Create or refresh the Finance notification after Manager approval.

        Every Scrap reaches this stage only after Manager approval has moved
        the record from PENDING_MANAGER to PENDING_FINANCE.
        """
        current = str(
            getattr(
                instance,
                "approval_status",
                "",
            )
            or ""
        ).strip().upper()

        if current != "PENDING_FINANCE":
            print(
                "SCRAP FINANCE NOTIFICATION SKIPPED:",
                cls.get_scrap_reference(instance),
                "- current state:",
                current or "UNKNOWN",
            )
            return None

        requester_name = (
            str(
                requester_name
                or "User"
            ).strip()
            or "User"
        )

        remarks = (
            str(
                getattr(
                    instance,
                    "remarks",
                    "",
                )
                or "Scrap"
            ).strip()
            or "Scrap"
        )

        notification, created = (
            Notification.objects
            .get_or_create(
                category="SCRAP",
                receiver="FINANCE",
                reference_id=str(
                    instance.pk
                ),
                defaults={
                    "title": requester_name,
                    "requested_by": requester_name,
                    "message": remarks,
                    "status": "PENDING_FINANCE",
                    "is_read": False,
                },
            )
        )

        if not created:
            notification.title = requester_name
            notification.requested_by = requester_name
            notification.message = remarks
            notification.status = "PENDING_FINANCE"
            notification.is_read = False

            notification.save(
                update_fields=[
                    "title",
                    "requested_by",
                    "message",
                    "status",
                    "is_read",
                ]
            )

        return notification

    @classmethod
    def ensure_scrap_manager_notification(
        cls,
        instance,
        requester_name,
    ):
        """
        Backend becomes the source of truth for the Manager Scrap
        notification. Existing frontends already check whether the
        notification exists, so they will not create duplicates.
        """
        current = str(
            getattr(
                instance,
                "approval_status",
                "",
            )
            or ""
        ).strip().upper()

        # HARD WORKFLOW GUARD:
        # Manager is the FIRST approval stage. Manager notifications are
        # valid only while the Scrap is PENDING_MANAGER.
        if current != "PENDING_MANAGER":
            print(
                "SCRAP MANAGER NOTIFICATION SKIPPED:",
                cls.get_scrap_reference(instance),
                "- current state:",
                current or "UNKNOWN",
            )
            return None

        requester_name = (
            str(
                requester_name
                or "User"
            ).strip()
            or "User"
        )

        remarks = (
            str(
                getattr(
                    instance,
                    "remarks",
                    "",
                )
                or "Scrap"
            ).strip()
            or "Scrap"
        )

        notification, created = (
            Notification.objects
            .get_or_create(
                category="SCRAP",
                receiver="MANAGER",
                reference_id=str(
                    instance.pk
                ),
                defaults={
                    "title":
                        requester_name,
                    "requested_by":
                        requester_name,
                    "message":
                        remarks,
                    "status":
                        "PENDING_MANAGER",
                    "is_read":
                        False,
                },
            )
        )

        if not created:
            notification.title = (
                requester_name
            )
            notification.requested_by = (
                requester_name
            )
            notification.message = remarks
            notification.status = (
                "PENDING_MANAGER"
            )
            notification.is_read = False

            notification.save(
                update_fields=[
                    "title",
                    "requested_by",
                    "message",
                    "status",
                    "is_read",
                ]
            )

        return notification

    @classmethod
    def ensure_scrap_creator_notification(
        cls,
        instance,
        actor_name,
        *,
        actor_role="Manager",
        outcome="approved",
        rejection_reason="",
    ):
        """
        Create the FINAL Scrap result notification for only the exact user
        who originally created the Scrap.

        This helper is called only when the workflow has ended:
            - Manager rejected, or
            - Finance approved/rejected.

        No email is sent from this helper.
        """
        requester_user = (
            cls.resolve_scrap_requester_user(
                instance
            )
        )

        if not requester_user:
            print(
                "SCRAP CREATOR NOTIFICATION SKIPPED:",
                cls.get_scrap_reference(instance),
                "- original requester could not be resolved.",
            )
            return None

        normalized_outcome = str(
            outcome or "approved"
        ).strip().lower()

        is_rejected = (
            normalized_outcome == "rejected"
        )

        normalized_actor_role = (
            str(actor_role or "User").strip()
            or "User"
        )

        scrap_reference = (
            cls.get_scrap_reference(instance)
        )

        requester_name = (
            cls.get_mail_user_name(
                requester_user,
                getattr(
                    instance,
                    "requested_by",
                    "",
                )
                or "User",
            )
        )

        current_status = str(
            getattr(
                instance,
                "approval_status",
                "",
            )
            or ""
        ).strip().upper()

        if is_rejected:
            final_status = (
                current_status
                if current_status in {
                    "MANAGER_REJECTED",
                    "FINANCE_REJECTED",
                }
                else "REJECTED"
            )
            title = (
                f"Scrap Rejected - {scrap_reference}"
            )
            reason = str(
                rejection_reason or ""
            ).strip()
            message = (
                f"Your Scrap request {scrap_reference} "
                f"was rejected by {normalized_actor_role} "
                f"{actor_name or normalized_actor_role}."
            )
            if reason:
                message += f" Reason: {reason}"
        else:
            final_status = (
                current_status
                if current_status == "APPROVED"
                else "APPROVED"
            )
            title = (
                f"Scrap Approved - {scrap_reference}"
            )
            message = (
                f"Your Scrap request {scrap_reference} "
                f"has been approved by {normalized_actor_role} "
                f"{actor_name or normalized_actor_role}."
            )

        notification, created = (
            Notification.objects.get_or_create(
                category="SCRAP",
                recipient_user=requester_user,
                reference_id=str(instance.pk),
                status=final_status,
                defaults={
                    "title": title,
                    "requested_by": requester_name,
                    "message": message,
                    "receiver": None,
                    "is_read": False,
                },
            )
        )

        if not created:
            notification.title = title
            notification.requested_by = (
                requester_name
            )
            notification.message = message
            notification.receiver = None
            notification.is_read = False
            notification.save(
                update_fields=[
                    "title",
                    "requested_by",
                    "message",
                    "receiver",
                    "is_read",
                ]
            )

        return notification

    @classmethod
    def send_scrap_finance_approval_email(
        cls,
        outward_id,
    ):
        """
        Send a Scrap approval-request email to every active Finance user.
        This email is sent only AFTER Manager approval.
        """
        try:
            instance = (
                OutwardEntry.objects
                .select_related(
                    "component",
                    "material_request",
                )
                .get(pk=outward_id)
            )
        except OutwardEntry.DoesNotExist:
            return False

        current = str(
            getattr(
                instance,
                "approval_status",
                "",
            )
            or ""
        ).strip().upper()

        # Finance mail is valid only at the Finance stage.
        if current != "PENDING_FINANCE":
            print(
                "SCRAP FINANCE EMAIL SKIPPED:",
                cls.get_scrap_reference(instance),
                "- current state:",
                current or "UNKNOWN",
            )
            return False

        # ---------------------------------------------------------
        # STRICT FINANCE RECIPIENT SELECTION
        # ---------------------------------------------------------
        # Do not trust a single ORM role filter here. In this project some
        # user payloads can expose role through a related object/name, and
        # the same email address may also exist on another active account.
        #
        # Rules for the Finance-stage Scrap email:
        #   1. recipient must resolve to role == FINANCE
        #   2. recipient email must NOT be used by any active MANAGER
        #   3. send only once per unique Finance email address
        #
        # This keeps Finance-stage delivery limited to Finance recipients.
        active_users_with_email = list(
            User.objects
            .filter(is_active=True)
            .exclude(email__isnull=True)
            .exclude(email="")
            .order_by("id")
        )

        manager_email_keys = {
            str(user.email or "").strip().casefold()
            for user in active_users_with_email
            if (
                cls.user_has_exact_role(user, "manager")
                and str(user.email or "").strip()
            )
        }

        finance_users = []
        seen_finance_email_keys = set()

        for candidate in active_users_with_email:
            if not cls.user_has_exact_role(candidate, "finance"):
                roles = cls.get_user_role_candidates(candidate)
                if "finance" in roles:
                    print(
                        "SCRAP FINANCE RECIPIENT BLOCKED DUE TO ROLE CONFLICT:",
                        candidate.pk,
                        candidate.email,
                        sorted(roles),
                    )
                continue

            email_key = str(
                candidate.email or ""
            ).strip().casefold()

            if not email_key:
                continue

            # If this mailbox is also attached to an active Manager, never
            # use it for the Finance-stage Scrap email.
            if email_key in manager_email_keys:
                print(
                    "SCRAP FINANCE RECIPIENT BLOCKED:",
                    candidate.pk,
                    candidate.email,
                    "- same email is used by an active Manager.",
                )
                continue

            if email_key in seen_finance_email_keys:
                continue

            seen_finance_email_keys.add(email_key)
            finance_users.append(candidate)

        if not finance_users:
            print(
                "SCRAP FINANCE EMAIL SKIPPED:",
                cls.get_scrap_reference(
                    instance
                ),
                "- no eligible Finance-only user with email.",
            )
            return False

        requester_user = (
            cls.resolve_scrap_requester_user(
                instance
            )
        )

        requester_name = (
            cls.get_mail_user_name(
                requester_user,
                getattr(
                    instance,
                    "requested_by",
                    "",
                )
                or "User",
            )
        )

        material_request = getattr(
            instance,
            "material_request",
            None,
        )

        mr_number = (
            getattr(
                material_request,
                "material_request_id",
                "",
            )
            or "-"
        )

        source = str(
            getattr(
                instance,
                "source",
                "",
            )
            or "DIRECT"
        ).strip().upper()

        scrap_reference = (
            cls.get_scrap_reference(
                instance
            )
        )

        component_label = (
            cls.get_scrap_component_label(
                instance
            )
        )

        subject = (
            f"{scrap_reference} submitted by "
            f"{requester_name} - Finance Approval Required"
        )

        sent_any = False

        for finance in finance_users:
            sent = send_ipms_email(
                recipient_email=finance.email,
                subject=subject,
                context={
                    "recipient_name":
                        cls.get_mail_user_name(
                            finance,
                            "Finance",
                        ),
                    "message": (
                        f"A Scrap request submitted by "
                        f"{requester_name} has been "
                        f"approved by Manager and now "
                        f"requires Finance approval."
                    ),
                    "table_headers": [
                        "Scrap Reference",
                        "Requested By",
                        "Source",
                        "Material Request",
                        "Component",
                        "Quantity",
                        "Scrap Date",
                        "Remarks",
                        "Status",
                    ],
                    "table_values": [
                        scrap_reference,
                        requester_name,
                        source,
                        mr_number,
                        component_label,
                        int(
                            getattr(
                                instance,
                                "quantity",
                                0,
                            )
                            or 0
                        ),
                        getattr(
                            instance,
                            "out_date",
                            "",
                        )
                        or "-",
                        getattr(
                            instance,
                            "remarks",
                            "",
                        )
                        or "-",
                        "Pending Finance Approval",
                    ],
                    "status":
                        "Pending Finance Approval",
                    "instruction": (
                        "Please review this Scrap "
                        "request from Finance "
                        "Notifications in IPMS."
                    ),
                    "button_text":
                        "Review Scrap in IPMS",
                    "action_url": (
                        f"{cls.get_ipms_base_url()}"
                        f"/notifications"
                    ),
                },
            )

            if sent:
                sent_any = True

        print(
            "SCRAP FINANCE EMAIL SENT =",
            sent_any,
            "| SCRAP =",
            scrap_reference,
            "| SOURCE =",
            source,
        )

        return sent_any

    @classmethod
    def send_scrap_manager_approval_email(
        cls,
        outward_id,
    ):
        """
        Send the FIRST Scrap approval email to Manager.
        The PENDING_MANAGER guard prevents duplicate/stale stage emails.
        """
        try:
            instance = (
                OutwardEntry.objects
                .select_related(
                    "component",
                    "material_request",
                )
                .get(pk=outward_id)
            )
        except OutwardEntry.DoesNotExist:
            return False

        current = str(
            getattr(
                instance,
                "approval_status",
                "",
            )
            or ""
        ).strip().upper()

        # HARD WORKFLOW GUARD:
        # Manager is the first approval stage, so this mail is valid only
        # while the newly-created Scrap is PENDING_MANAGER.
        if current != "PENDING_MANAGER":
            print(
                "SCRAP MANAGER EMAIL SKIPPED:",
                cls.get_scrap_reference(instance),
                "- current state:",
                current or "UNKNOWN",
            )
            return False

        # ---------------------------------------------------------
        # STRICT MANAGER RECIPIENT SELECTION
        # ---------------------------------------------------------
        # This function is protected by PENDING_MANAGER above. Resolve the
        # Manager role in Python
        # with the same helper used by permission checks, and de-duplicate by
        # email so each Manager mailbox receives one approval-request mail.
        active_users_with_email = list(
            User.objects
            .filter(is_active=True)
            .exclude(email__isnull=True)
            .exclude(email="")
            .order_by("id")
        )

        managers = []
        seen_manager_email_keys = set()

        for candidate in active_users_with_email:
            if not cls.user_has_exact_role(candidate, "manager"):
                roles = cls.get_user_role_candidates(candidate)
                if "manager" in roles:
                    print(
                        "SCRAP MANAGER RECIPIENT BLOCKED DUE TO ROLE CONFLICT:",
                        candidate.pk,
                        candidate.email,
                        sorted(roles),
                    )
                continue

            email_key = str(
                candidate.email or ""
            ).strip().casefold()

            if (
                not email_key
                or email_key in seen_manager_email_keys
            ):
                continue

            seen_manager_email_keys.add(email_key)
            managers.append(candidate)

        if not managers:
            print(
                "SCRAP MANAGER EMAIL SKIPPED:",
                cls.get_scrap_reference(
                    instance
                ),
                "- no active Manager user with email.",
            )
            return False

        requester_user = (
            cls.resolve_scrap_requester_user(
                instance
            )
        )

        requester_name = (
            cls.get_mail_user_name(
                requester_user,
                getattr(
                    instance,
                    "requested_by",
                    "",
                )
                or "User",
            )
        )

        material_request = getattr(
            instance,
            "material_request",
            None,
        )

        mr_number = (
            getattr(
                material_request,
                "material_request_id",
                "",
            )
            or "-"
        )

        source = str(
            getattr(
                instance,
                "source",
                "",
            )
            or "DIRECT"
        ).strip().upper()

        scrap_reference = (
            cls.get_scrap_reference(
                instance
            )
        )

        component_label = (
            cls.get_scrap_component_label(
                instance
            )
        )

        subject = (
            f"{scrap_reference} submitted by "
            f"{requester_name} - Approval Required"
        )

        sent_any = False

        for manager in managers:
            sent = send_ipms_email(
                recipient_email=manager.email,
                subject=subject,
                context={
                    "recipient_name":
                        cls.get_mail_user_name(
                            manager,
                            "Manager",
                        ),
                    "message": (
                        f"A Scrap request was "
                        f"submitted by "
                        f"{requester_name} and "
                        f"requires Manager approval."
                    ),
                    "table_headers": [
                        "Scrap Reference",
                        "Requested By",
                        "Source",
                        "Material Request",
                        "Component",
                        "Quantity",
                        "Scrap Date",
                        "Remarks",
                        "Status",
                    ],
                    "table_values": [
                        scrap_reference,
                        requester_name,
                        source,
                        mr_number,
                        component_label,
                        int(
                            getattr(
                                instance,
                                "quantity",
                                0,
                            )
                            or 0
                        ),
                        getattr(
                            instance,
                            "out_date",
                            "",
                        )
                        or "-",
                        getattr(
                            instance,
                            "remarks",
                            "",
                        )
                        or "-",
                        "Pending Manager Approval",
                    ],
                    "status":
                        "Pending Manager Approval",
                    "instruction": (
                        "Please review this Scrap "
                        "request from Manager "
                        "Notifications in IPMS."
                    ),
                    "button_text":
                        "Review Scrap in IPMS",
                    "action_url": (
                        f"{cls.get_ipms_base_url()}"
                        f"/notifications"
                    ),
                },
            )

            if sent:
                sent_any = True

        print(
            "SCRAP MANAGER EMAIL SENT =",
            sent_any,
            "| SCRAP =",
            scrap_reference,
            "| SOURCE =",
            source,
        )

        return sent_any

    @classmethod
    def register_new_scrap_workflow(
        cls,
        instance,
        user,
    ):
        """
        Called only after a new Scrap OutwardEntry exists.

        Required approval order:
            PENDING_MANAGER -> PENDING_FINANCE -> APPROVED

        A newly-created Scrap is sent to Manager first. Finance receives a
        notification/email only after manager_approve() succeeds.
        """
        if (
            str(
                getattr(
                    instance,
                    "outward_type",
                    "",
                )
                or ""
            ).strip().upper()
            != "SCRAP"
        ):
            return

        authenticated_user = (
            user
            if (
                user
                and getattr(
                    user,
                    "is_authenticated",
                    False,
                )
            )
            else None
        )

        requester_name = (
            cls.get_actor_name(
                authenticated_user
            )
            if authenticated_user
            else (
                str(
                    getattr(
                        instance,
                        "requested_by",
                        "",
                    )
                    or "User"
                ).strip()
                or "User"
            )
        )

        update_fields = []

        instance.requested_by = requester_name
        update_fields.append(
            "requested_by"
        )

        if authenticated_user:
            instance.requested_by_user_id = (
                authenticated_user.pk
            )
            update_fields.append(
                "requested_by_user_id"
            )

        if not str(
            getattr(
                instance,
                "source",
                "",
            )
            or ""
        ).strip():
            instance.source = "DIRECT"
            update_fields.append(
                "source"
            )

        # All newly-created Scrap enters Manager approval first.
        instance.approval_status = (
            "PENDING_MANAGER"
        )
        instance.status = "PENDING_MANAGER"
        instance.rejection_reason = None
        instance.rejected_by = None

        update_fields.extend(
            [
                "approval_status",
                "status",
                "rejection_reason",
                "rejected_by",
            ]
        )

        instance.save(
            update_fields=list(
                dict.fromkeys(
                    update_fields
                    + ["updated_at"]
                )
            )
        )

        # Manager is the first approval stage.
        cls.ensure_scrap_manager_notification(
            instance,
            requester_name,
        )

        transaction.on_commit(
            lambda outward_id=instance.pk: (
                cls.send_scrap_manager_approval_email(
                    outward_id
                )
            )
        )

    @staticmethod
    def get_actor_name(user):
        full_name = ""

        try:
            full_name = user.get_full_name()
        except Exception:
            full_name = ""

        value = str(
            getattr(
                user,
                "employee_name",
                "",
            )
            or getattr(
                user,
                "name",
                "",
            )
            or full_name
            or getattr(
                user,
                "username",
                "",
            )
            or getattr(
                user,
                "email",
                "",
            )
            or "User"
        ).strip()

        # UI requirement: keep display/audit name, never a full email.
        if "@" in value:
            value = (
                value.split("@")[0]
                .strip()
                or "User"
            )

        return value[:50]

    @classmethod
    def get_actor_identity_names(cls, user):
        values = []
        for attribute in (
            "employee_name", "name", "full_name", "username", "email"
        ):
            value = getattr(user, attribute, "")
            if value:
                values.append(value)
        try:
            full_name = user.get_full_name()
            if full_name:
                values.append(full_name)
        except Exception:
            pass
        values.append(cls.get_actor_name(user))
        result = set()
        for value in values:
            normalized = str(value or "").strip()
            if not normalized:
                continue
            result.add(normalized.casefold())
            if "@" in normalized:
                result.add(normalized.split("@")[0].strip().casefold())
        return {value for value in result if value}

    @classmethod
    def material_request_belongs_to_user(cls, material_request, user):
        if cls.is_admin_user(user):
            return True
        requester_name = str(
            getattr(material_request, "requester_name", "") or ""
        ).strip().casefold()
        return requester_name in cls.get_actor_identity_names(user)

    @staticmethod
    def get_engineer_in_drone_statuses():
        return {
            "INVENTORY_ISSUED",
            "MR_COMPLETED",
            "ISSUED",
            "COMPLETED",
        }

    @classmethod
    def get_material_request_from_reference(cls, reference, *, lock=False):
        raw = str(reference or "").strip()
        if not raw:
            return None
        queryset = MaterialRequest.objects.all()
        if lock:
            queryset = queryset.select_for_update()
        lookup = Q(material_request_id=raw)
        if raw.isdigit():
            lookup |= Q(pk=int(raw))
        return queryset.filter(lookup).first()

    @classmethod
    def get_project_rows_for_mr(cls, material_request, *, lock=False):
        queryset = (
            ProjectInventory.objects
            .select_related("material_request", "component")
            .filter(material_request=material_request)
            .order_by("component_id", "id")
        )
        if lock:
            queryset = queryset.select_for_update()
        return list(queryset)

    @classmethod
    def get_used_mr_scrap_serials(
        cls,
        material_request,
        *,
        lock=False,
    ):
        """
        Serials unavailable to a NEW Engineer Scrap request.

        Active Scrap rows reserve:
        - exact serials selected for Scrap
        - for Partial Scrap + Reordering NO, the remaining good serials
          that will return to central Inventory after final approval

        Rejected Scrap rows release their serials again.
        """
        queryset = (
            OutwardEntry.objects
            .filter(
                outward_type="SCRAP",
                scrap_origin="MR",
                material_request=material_request,
            )
            .order_by("id")
        )

        if lock:
            queryset = queryset.select_for_update()

        unavailable = set()

        rejected_states = {
            "REJECTED",
            "MANAGER_REJECTED",
            "FINANCE_REJECTED",
        }

        for row in queryset:
            approval_state = str(
                row.approval_status or ""
            ).strip().upper()

            status_state = str(
                row.status or ""
            ).strip().upper()

            if (
                approval_state in rejected_states
                or status_state in rejected_states
            ):
                continue

            unavailable.update(
                cls.normalize_serials(
                    row.serial_numbers
                )
            )

            metadata = (
                row.inventory_allocations
                if isinstance(
                    row.inventory_allocations,
                    dict,
                )
                else {}
            )

            if (
                metadata.get("workflow")
                == "ENGINEER_MR_SCRAP_DISPOSITION_V1"
            ):
                for item in (
                    metadata.get(
                        "return_items",
                        [],
                    )
                    or []
                ):
                    if not isinstance(item, dict):
                        continue

                    unavailable.update(
                        cls.normalize_serials(
                            item.get(
                                "serial_numbers"
                            )
                            or []
                        )
                    )

        return unavailable

    @classmethod
    def get_engineer_scrap_component_snapshot(
        cls,
        material_request,
        *,
        lock=False,
    ):
        """
        Return exact issued serials available for the Scrap disposition.

        ProjectInventory is the source of truth for what was issued to
        the INVENTORY_ISSUED MR.
        """
        project_rows = cls.get_project_rows_for_mr(
            material_request,
            lock=lock,
        )

        unavailable = cls.get_used_mr_scrap_serials(
            material_request,
            lock=lock,
        )

        result = []

        for project_row in project_rows:
            issued_serials = cls.normalize_serials(
                cls.normalize_serials(
                    project_row.issued_store_serials
                )
                + cls.normalize_serials(
                    project_row.issued_purchased_serials
                )
            )

            if not issued_serials:
                continue

            available_serials = [
                serial
                for serial in issued_serials
                if serial not in unavailable
            ]

            if not available_serials:
                continue

            component = project_row.component

            component_code = str(
                getattr(
                    component,
                    "component_id",
                    "",
                )
                or ""
            ).strip()

            component_name = str(
                getattr(
                    component,
                    "name",
                    "",
                )
                or ""
            ).strip()

            label = (
                " - ".join(
                    value
                    for value in [
                        component_code,
                        component_name,
                    ]
                    if value
                )
                or component_name
                or component_code
                or f"Component {project_row.component_id}"
            )

            result.append(
                {
                    "component":
                        project_row.component_id,
                    "component_code":
                        component_code,
                    "component_name":
                        component_name,
                    "label":
                        label,
                    "issued_serials":
                        issued_serials,
                    "available_serials":
                        available_serials,
                    "issued_quantity":
                        int(
                            project_row
                            .calculated_issued_quantity
                            or 0
                        ),
                    "requested_quantity":
                        int(
                            project_row
                            .requested_quantity
                            or 0
                        ),
                    "issue_status":
                        "ISSUED",
                }
            )

        return result

    @classmethod
    def build_engineer_scrap_mr_options(
        cls,
        user,
    ):
        """
        Engineer Scrap dropdown rule:

        ONLY Material Requests whose current status is exactly
        INVENTORY_ISSUED are displayed.

        Each option exposes the exact issued serials that are still
        available for a new Scrap disposition.
        """
        # An INVENTORY_ISSUED MR can be selected for Engineer Scrap
        # only once at a time.
        #
        # As soon as a non-rejected Engineer Scrap request exists for the MR,
        # hide that MR from the dropdown. This prevents the same issued MR
        # from being selected again while it is Pending Manager / Pending
        # Finance / Approved / Moved to Store.
        #
        # If the Scrap request is rejected, the MR becomes selectable again.
        blocked_mr_ids = (
            OutwardEntry.objects
            .filter(
                source="ENGINEER",
                outward_type="SCRAP",
                scrap_origin="MR",
                material_request_id__isnull=False,
            )
            .exclude(
                approval_status__in=[
                    "REJECTED",
                    "MANAGER_REJECTED",
                    "FINANCE_REJECTED",
                ]
            )
            .exclude(
                status__in=[
                    "REJECTED",
                    "MANAGER_REJECTED",
                    "FINANCE_REJECTED",
                ]
            )
            .values_list(
                "material_request_id",
                flat=True,
            )
            .distinct()
        )

        mr_queryset = (
            MaterialRequest.objects
            .filter(
                status="INVENTORY_ISSUED"
            )
            .exclude(
                id__in=blocked_mr_ids
            )
            .order_by(
                "-date",
                "-id",
            )
        )

        options = []

        for material_request in mr_queryset:
            components = (
                cls.get_engineer_scrap_component_snapshot(
                    material_request
                )
            )

            if not components:
                continue

            total_available_quantity = sum(
                len(
                    item.get(
                        "available_serials",
                        [],
                    )
                    or []
                )
                for item in components
            )

            if total_available_quantity <= 0:
                continue

            options.append(
                {
                    "id":
                        material_request.id,
                    "material_request_id":
                        material_request
                        .material_request_id,
                    "requester_name":
                        material_request
                        .requester_name,
                    "project":
                        material_request
                        .project,
                    "date":
                        material_request
                        .date,
                    "status":
                        material_request
                        .status,
                    "total_available_quantity":
                        total_available_quantity,
                    "components":
                        components,
                }
            )

        return options

    @classmethod
    def normalize_scrap_items(
        cls,
        raw_items,
    ):
        """
        Normalize frontend scrap_items into:
        [{component, serial_numbers, quantity}, ...]
        """
        if not isinstance(raw_items, list):
            return []

        normalized = []

        for raw in raw_items:
            if not isinstance(raw, dict):
                continue

            component_id = (
                raw.get("component")
                or raw.get("component_id")
            )

            serials = cls.normalize_serials(
                raw.get("serial_numbers")
                or raw.get("selected_serials")
                or raw.get("serials")
                or []
            )

            if not component_id or not serials:
                continue

            normalized.append(
                {
                    "component":
                        component_id,
                    "serial_numbers":
                        serials,
                    "quantity":
                        len(serials),
                }
            )

        return normalized

    @staticmethod
    def get_source_mr_item(
        material_request,
        component_id,
    ):
        request_type = str(
            material_request.request_type
            or ""
        ).strip().upper()

        manager = (
            material_request.rd_items
            if request_type in {"R&D", "RD"}
            else material_request.bom_items
        )

        return (
            manager
            .filter(
                component_id=component_id
            )
            .order_by("id")
            .first()
        )

    @staticmethod
    def generate_scrap_return_inventory_code():
        """
        Unique, audit-friendly Inventory code for usable components
        returned from an issued MR after Scrap disposition.
        """
        while True:
            code = (
                "INVRET-"
                + timezone.now().strftime(
                    "%Y%m%d%H%M%S%f"
                )
                + "-"
                + uuid4().hex[:6].upper()
            )

            if not Inventory.objects.filter(
                inventory_code=code
            ).exists():
                return code

    @classmethod
    def create_scrap_return_inventory(
        cls,
        *,
        source_mr,
        item,
    ):
        serials = cls.normalize_serials(
            item.get("serial_numbers")
            or []
        )

        if not serials:
            return None

        component_id = item.get(
            "component"
        )

        project_row = (
            ProjectInventory.objects
            .select_related("component")
            .filter(
                material_request=source_mr,
                component_id=component_id,
            )
            .first()
        )

        if not project_row:
            return None

        component = project_row.component

        source_item = cls.get_source_mr_item(
            source_mr,
            component_id,
        )

        unit_price = Decimal(
            str(
                getattr(
                    source_item,
                    "unit_price",
                    0,
                )
                or 0
            )
        )

        total_price = (
            unit_price
            * Decimal(
                len(serials)
            )
        )

        return Inventory.objects.create(
            inventory_code=(
                cls.generate_scrap_return_inventory_code()
            ),
            component=component,
            category=(
                getattr(
                    component,
                    "category",
                    "",
                )
                or getattr(
                    source_item,
                    "category",
                    "",
                )
                or ""
            ),
            vendor=(
                "Returned from issued MR"
            ),
            purchase_order=(
                f"SCRAP-RETURN:"
                f"{source_mr.material_request_id}"
            ),
            quantity=len(serials),
            received_date=timezone.localdate(),
            total_price=total_price,
            issued=False,
            serial_numbers=serials,
            issued_serial_numbers=[],
        )

    @classmethod
    def generate_next_material_request_id(
        cls,
    ):
        """
        Generate the SAME MR ID format used by the New Material Request page:

            MR-YYMMDD-00001

        Example:
            MR-260825-00001

        The sequence is shared across normal MRs and From-Scrap MRs for the
        same date, so a Scrap-created MR continues the normal MR numbering.
        """
        date_part = (
            timezone.localdate()
            .strftime("%y%m%d")
        )

        prefix = f"MR-{date_part}-"

        existing_ids = (
            MaterialRequest.objects
            .filter(
                material_request_id__startswith=
                    prefix
            )
            .values_list(
                "material_request_id",
                flat=True,
            )
        )

        highest_sequence = 0

        for request_id in existing_ids:
            value = str(
                request_id or ""
            ).strip()

            if not value.startswith(
                prefix
            ):
                continue

            sequence_text = value[
                len(prefix):
            ]

            if (
                len(sequence_text) != 5
                or not sequence_text.isdigit()
            ):
                continue

            highest_sequence = max(
                highest_sequence,
                int(sequence_text),
            )

        return (
            f"{prefix}"
            f"{highest_sequence + 1:05d}"
        )


    @classmethod
    def create_scrap_reorder_mr(
        cls,
        *,
        scrap_entry,
        source_mr,
        scrap_items,
    ):
        """
        Create a dedicated FROM-SCRAP Material Request.

        IMPORTANT:
        - This is NOT a BOM / Custom BOM / R&D procurement request.
        - The physical components already come from the scrapped drone.
        - The selected serial numbers are carried directly into this MR.
        - Manager approval must NOT reserve In-Store stock and must NOT
          create Project Inventory / Procurement work for this request.

        No schema migration is required. The existing CharField stores the
        internal request_type value "SCRAP".
        """
        requester = None

        if scrap_entry.requested_by_user_id:
            requester = (
                User.objects
                .filter(
                    pk=
                        scrap_entry
                        .requested_by_user_id
                )
                .first()
            )

        requester_name = (
            scrap_entry.requested_by
            or getattr(
                requester,
                "employee_name",
                "",
            )
            or getattr(
                requester,
                "email",
                "",
            )
            or "Engineer"
        )

        total_quantity = sum(
            max(
                int(
                    item.get(
                        "quantity",
                        0,
                    )
                    or 0
                ),
                0,
            )
            for item in scrap_items
        )

        reorder_mr = MaterialRequest.objects.create(
            # Use the exact same ID format and daily sequence as the
            # normal New Material Request page.
            material_request_id=(
                cls.generate_next_material_request_id()
            ),

            requester_name=requester_name,
            requester=requester,
            date=timezone.localdate(),
            project=source_mr.project,

            # FROM-SCRAP is intentionally independent of BOM.
            bom=None,
            customized_bom=False,
            request_type="SCRAP",

            required_quantity=max(
                total_quantity,
                1,
            ),
            required_date=(
                source_mr.required_date
            ),
            remarks=(
                f"Automatically created From Scrap for "
                f"{source_mr.material_request_id}. "
                f"Selected quantity: "
                f"{total_quantity}."
            ),
            status="PENDING_MANAGER",
            approval_status="PENDING_MANAGER",
            po_raised=False,
        )

        for item in scrap_items:
            component_id = item.get(
                "component"
            )

            serial_numbers = (
                cls.normalize_serials(
                    item.get(
                        "serial_numbers"
                    )
                    or []
                )
            )

            # Selected quantity must match exact selected serials.
            quantity = len(serial_numbers)

            if (
                not component_id
                or quantity <= 0
            ):
                continue

            source_item = (
                cls.get_source_mr_item(
                    source_mr,
                    component_id,
                )
            )

            if source_item is None:
                continue

            component = getattr(
                source_item,
                "component",
                None,
            )

            if component is None:
                continue

            unit_price = getattr(
                source_item,
                "unit_price",
                0,
            ) or 0

            price = (
                Decimal(
                    str(unit_price)
                )
                * Decimal(quantity)
            )

            # Always store FROM-SCRAP rows in BOMItem.
            # This is only a detail container; it is NOT a BOM workflow.
            BOMItem.objects.create(
                material_request=reorder_mr,
                component=component,
                category=(
                    getattr(
                        source_item,
                        "category",
                        "",
                    )
                    or getattr(
                        component,
                        "category",
                        "",
                    )
                    or ""
                ),
                specification=(
                    getattr(
                        source_item,
                        "specification",
                        "",
                    )
                    or getattr(
                        source_item,
                        "specifications",
                        "",
                    )
                    or getattr(
                        component,
                        "specifications",
                        "",
                    )
                    or ""
                ),
                quantity=quantity,
                unit=(
                    getattr(
                        source_item,
                        "unit",
                        "pc",
                    )
                    or "pc"
                ),
                unit_price=unit_price,
                price=price,
                tax=(
                    getattr(
                        source_item,
                        "tax",
                        0,
                    )
                    or 0
                ),

                # Do NOT treat these items as coming from In Store.
                inventory_quantity=0,
                po_raised_quantity=0,
                delivered_quantity=0,
                qc_passed_quantity=0,
                qc_failed_quantity=0,
                project_inventory_quantity=0,

                vendor=None,

                # Persist exact selected serials without a new DB field.
                # MaterialRequestsPage parses this marker for the
                # dedicated "From Scrap Details" popup.
                remarks=(
                    f"FROM_SCRAP_SERIALS:"
                    f"{'|'.join(serial_numbers)}"
                    f"\nSOURCE_SCRAP:{scrap_entry.code}"
                    f"\nSOURCE_MR:"
                    f"{source_mr.material_request_id}"
                ),
            )

        # Keep the existing Manager notification stage.
        Notification.objects.update_or_create(
            category="MR",
            receiver="MANAGER",
            reference_id=str(
                reorder_mr.id
            ),
            defaults={
                "title": (
                    "MR Approval Request - "
                    f"{reorder_mr.material_request_id}"
                ),
                "message": (
                    f"From Scrap Material Request "
                    f"{reorder_mr.material_request_id} "
                    f"requires manager approval."
                ),
                "status":
                    "PENDING_MANAGER",
                "is_read":
                    False,
            },
        )

        return reorder_mr


    @classmethod
    def send_scrap_reorder_mr_manager_email(
        cls,
        material_request_id,
    ):
        try:
            material_request = (
                MaterialRequest.objects
                .select_related("requester")
                .get(pk=material_request_id)
            )
        except MaterialRequest.DoesNotExist:
            return False

        managers = (
            User.objects
            .filter(
                role__iexact="manager",
                is_active=True,
            )
            .exclude(
                email__isnull=True
            )
            .exclude(email="")
            .order_by("id")
        )

        requester_name = (
            material_request.requester_name
            or "Engineer"
        )

        sent_any = False

        for manager in managers:
            sent = send_ipms_email(
                recipient_email=manager.email,
                subject=(
                    f"{material_request.material_request_id} "
                    f"submitted by {requester_name} "
                    f"- Approval Required"
                ),
                context={
                    "recipient_name":
                        cls.get_mail_user_name(
                            manager,
                            "Manager",
                        ),
                    "message": (
                        "A new Material Request was "
                        "automatically created from an "
                        "approved Engineer Scrap reordering "
                        "decision and is awaiting your approval."
                    ),
                    "table_headers": [
                        "MR ID",
                        "Request Type",
                        "Project",
                        "Submitted By",
                        "Required Date",
                        "Status",
                    ],
                    "table_values": [
                        material_request
                        .material_request_id,
                        (
                            "From Scrap"
                            if str(
                                material_request
                                .request_type
                                or ""
                            ).strip().upper()
                            == "SCRAP"
                            else material_request
                            .request_type
                        ),
                        material_request
                        .project,
                        requester_name,
                        (
                            material_request
                            .required_date
                            .strftime(
                                "%d/%m/%Y"
                            )
                            if material_request
                            .required_date
                            else "-"
                        ),
                        "Pending Manager",
                    ],
                    "status":
                        "Pending Manager",
                    "instruction": (
                        "Please review the request and "
                        "take the appropriate action in IPMS."
                    ),
                    "button_text":
                        "Review Request in IPMS",
                    "action_url": (
                        f"{cls.get_ipms_base_url()}"
                        f"/notifications"
                    ),
                },
            )

            if sent:
                sent_any = True

        return sent_any

    @classmethod
    def process_engineer_scrap_disposition(
        cls,
        *,
        scrap_entry,
    ):
        """
        Execute the NEW disposition only after final Finance approval
        and the Engineer clicks Move to Store.

        PARTIAL + YES:
            Unselected serials go to Scrap.
            Selected serial quantities create a NEW MR.

        PARTIAL + NO:
            Unselected serials go to Scrap.
            Selected serials return to central Inventory.

        TOTAL:
            Every available issued serial selected in this request is Scrap.
            No return and no automatic reorder.
        """
        metadata = (
            scrap_entry.inventory_allocations
            if isinstance(
                scrap_entry.inventory_allocations,
                dict,
            )
            else {}
        )

        if (
            metadata.get("workflow")
            != "ENGINEER_MR_SCRAP_DISPOSITION_V1"
        ):
            return metadata

        if metadata.get(
            "disposition_processed"
        ):
            return metadata

        source_mr = (
            MaterialRequest.objects
            .select_for_update()
            .get(
                pk=scrap_entry
                .material_request_id
            )
        )

        scrap_mode = str(
            metadata.get(
                "scrap_mode",
                "PARTIAL",
            )
        ).strip().upper()

        reorder_choice = str(
            metadata.get(
                "reorder_choice",
                "NONE",
            )
        ).strip().upper()

        scrap_items = (
            metadata.get(
                "scrap_items",
                [],
            )
            or []
        )

        return_items = (
            metadata.get(
                "return_items",
                [],
            )
            or []
        )

        reorder_items = (
            metadata.get(
                "reorder_items",
                [],
            )
            or []
        )

        returned_inventory_ids = []
        reorder_mr = None

        if (
            scrap_mode == "PARTIAL"
            and reorder_choice == "NO"
        ):
            for item in return_items:
                inventory_row = (
                    cls.create_scrap_return_inventory(
                        source_mr=source_mr,
                        item=item,
                    )
                )

                if inventory_row:
                    returned_inventory_ids.append(
                        inventory_row.id
                    )

        if (
            scrap_mode == "PARTIAL"
            and reorder_choice == "YES"
            and reorder_items
        ):
            reorder_mr = (
                cls.create_scrap_reorder_mr(
                    scrap_entry=scrap_entry,
                    source_mr=source_mr,
                    scrap_items=reorder_items,
                )
            )

        metadata["disposition_processed"] = True
        metadata["processed_at"] = (
            timezone.now().isoformat()
        )
        metadata[
            "returned_inventory_ids"
        ] = returned_inventory_ids
        metadata["replacement_mr_id"] = (
            reorder_mr.id
            if reorder_mr
            else None
        )
        metadata[
            "replacement_mr_number"
        ] = (
            reorder_mr.material_request_id
            if reorder_mr
            else ""
        )

        if reorder_mr:
            transaction.on_commit(
                lambda mr_id=reorder_mr.id: (
                    cls.send_scrap_reorder_mr_manager_email(
                        mr_id
                    )
                )
            )

        return metadata


    @staticmethod
    def normalize_serials(values):
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

    @classmethod
    def ensure_inventory_serials(cls, stock_row):
        serials = cls.normalize_serials(stock_row.serial_numbers)
        quantity = max(int(stock_row.quantity or 0), 0)

        prefix = "".join(
            character
            for character in str(
                stock_row.inventory_code or f"INV{stock_row.pk}"
            )
            if character.isalnum()
        ).upper() or f"INV{stock_row.pk}"

        used = set(serials) | set(
            cls.normalize_serials(stock_row.issued_serial_numbers)
        )
        index = 1

        while len(serials) < quantity:
            serial = f"CINV_{prefix}_S{index:05d}"
            index += 1
            if serial in used:
                continue
            used.add(serial)
            serials.append(serial)

        serials = serials[:quantity]

        if serials != cls.normalize_serials(stock_row.serial_numbers):
            stock_row.serial_numbers = serials
            stock_row.save(update_fields=["serial_numbers"])

        return serials

    @classmethod
    def deduct_component_stock(
        cls,
        *,
        component_id,
        quantity,
        selected_serials=None,
    ):
        requested = max(int(quantity or 0), 0)
        selected = cls.normalize_serials(selected_serials)

        if requested <= 0:
            raise ValidationError(
                {"quantity": "Quantity must be greater than zero."}
            )

        if selected and len(selected) != requested:
            raise ValidationError(
                {
                    "serial_numbers": (
                        "Selected serial count must equal the requested quantity."
                    )
                }
            )

        stock_rows = list(
            Inventory.objects
            .select_for_update()
            .select_related("component")
            .filter(
                component_id=component_id,
                issued=False,
                quantity__gt=0,
            )
            .order_by("received_date", "created_at", "id")
        )

        # Manager-approved MR reservations remain physically in Inventory
        # until issued, but Sales/Event must never consume that protected stock.
        reservation_rows = list(
            InventoryReservation.objects
            .select_for_update()
            .filter(component_id=component_id)
            .exclude(status__in=["RELEASED", "CANCELLED", "ISSUED"])
        )
        reserved_quantity = sum(
            max(
                int(row.reserved_store_quantity or 0)
                - int(row.issued_store_quantity or 0),
                0,
            )
            for row in reservation_rows
        )
        physical_quantity = sum(
            max(int(row.quantity or 0), 0)
            for row in stock_rows
        )
        free_quantity = max(
            physical_quantity - reserved_quantity,
            0,
        )

        if requested > free_quantity:
            raise ValidationError(
                {
                    "quantity": (
                        f"Only {free_quantity} unreserved item(s) are available "
                        "in In Store. The remaining stock is reserved for "
                        "Material Requests."
                    )
                }
            )

        available_by_serial = {}
        available_in_order = []

        for stock_row in stock_rows:
            for serial in cls.ensure_inventory_serials(stock_row):
                if serial not in available_by_serial:
                    available_by_serial[serial] = stock_row
                    available_in_order.append(serial)

        chosen = selected or available_in_order[:requested]

        missing = [
            serial
            for serial in chosen
            if serial not in available_by_serial
        ]
        if missing:
            raise ValidationError(
                {
                    "serial_numbers": (
                        "One or more serials are no longer available: "
                        + ", ".join(missing)
                    )
                }
            )

        if len(chosen) < requested:
            raise ValidationError(
                {
                    "quantity": (
                        f"Only {len(chosen)} item(s) are available in In Store; "
                        f"{requested} were requested."
                    )
                }
            )

        chosen_set = set(chosen)
        allocations = []
        actually_deducted = []

        for stock_row in stock_rows:
            current_serials = cls.ensure_inventory_serials(stock_row)
            row_serials = [
                serial
                for serial in current_serials
                if serial in chosen_set
            ]

            if not row_serials:
                continue

            row_serial_set = set(row_serials)
            remaining_serials = [
                serial
                for serial in current_serials
                if serial not in row_serial_set
            ]

            stock_row.quantity = len(remaining_serials)
            stock_row.serial_numbers = remaining_serials
            stock_row.issued_serial_numbers = cls.normalize_serials(
                cls.normalize_serials(stock_row.issued_serial_numbers)
                + row_serials
            )
            stock_row.issued = stock_row.quantity == 0
            stock_row.save(
                update_fields=[
                    "quantity",
                    "serial_numbers",
                    "issued_serial_numbers",
                    "issued",
                ]
            )

            allocations.append(
                {
                    "inventory_id": stock_row.id,
                    "inventory_code": stock_row.inventory_code,
                    "quantity": len(row_serials),
                    "serial_numbers": row_serials,
                }
            )
            actually_deducted.extend(row_serials)

        ordered_deducted = [
            serial
            for serial in chosen
            if serial in set(actually_deducted)
        ]

        if len(ordered_deducted) != requested:
            raise ValidationError(
                {
                    "quantity": (
                        "In-Store deduction was incomplete. Nothing was saved."
                    )
                }
            )

        return ordered_deducted, allocations

    @staticmethod
    def generate_return_inventory_code():
        stamp = timezone.now().strftime("%Y%m%d%H%M%S%f")
        return f"EVR-{stamp}-{uuid4().hex[:5].upper()}"

    @classmethod
    def get_return_stock_row(cls, outward, allocation):
        inventory_id = allocation.get("inventory_id")

        if inventory_id:
            stock_row = (
                Inventory.objects
                .select_for_update()
                .filter(pk=inventory_id)
                .first()
            )
            if stock_row is not None:
                return stock_row

        # Defensive fallback if an original Inventory row was deleted.
        return Inventory.objects.create(
            inventory_code=cls.generate_return_inventory_code(),
            component=outward.component,
            category=(
                getattr(outward.component, "category", "") or ""
            ),
            vendor="EVENT RETURN",
            purchase_order=outward.code,
            quantity=0,
            received_date=timezone.localdate(),
            total_price=0,
            issued=False,
            serial_numbers=[],
            issued_serial_numbers=[],
        )

    @classmethod
    def restore_event_component_stock(
        cls,
        outward,
        target_returned_quantity,
    ):
        total_quantity = max(int(outward.quantity or 0), 0)
        current_returned = max(
            int(outward.returned_quantity or 0),
            0,
        )
        target = max(int(target_returned_quantity or 0), 0)

        if target < current_returned:
            raise ValidationError(
                {
                    "returned_quantity": (
                        "Returned quantity cannot be reduced after stock has "
                        "already been restored."
                    )
                }
            )

        if target > total_quantity:
            raise ValidationError(
                {
                    "returned_quantity": (
                        f"Returned quantity cannot exceed Event quantity "
                        f"({total_quantity})."
                    )
                }
            )

        restore_count = target - current_returned
        existing_returned = cls.normalize_serials(
            outward.returned_serial_numbers
        )

        if restore_count == 0:
            return existing_returned

        issued_serials = cls.normalize_serials(outward.serial_numbers)
        available_to_restore = [
            serial
            for serial in issued_serials
            if serial not in set(existing_returned)
        ]
        serials_to_restore = available_to_restore[:restore_count]

        if len(serials_to_restore) != restore_count:
            raise ValidationError(
                {
                    "returned_quantity": (
                        "The Event row does not contain enough unreturned "
                        "serials to restore this quantity."
                    )
                }
            )

        allocations = (
            outward.inventory_allocations
            if isinstance(outward.inventory_allocations, list)
            else []
        )
        remaining = set(serials_to_restore)

        for allocation in allocations:
            if not isinstance(allocation, dict):
                continue

            allocation_serials = cls.normalize_serials(
                allocation.get("serial_numbers")
            )
            restore_for_row = [
                serial
                for serial in allocation_serials
                if serial in remaining
            ]

            if not restore_for_row:
                continue

            stock_row = cls.get_return_stock_row(
                outward,
                allocation,
            )
            current_serials = cls.ensure_inventory_serials(stock_row)
            current_issued = cls.normalize_serials(
                stock_row.issued_serial_numbers
            )

            for serial in restore_for_row:
                if serial not in current_serials:
                    current_serials.append(serial)

            restore_set = set(restore_for_row)
            current_issued = [
                serial
                for serial in current_issued
                if serial not in restore_set
            ]

            stock_row.serial_numbers = current_serials
            stock_row.quantity = len(current_serials)
            stock_row.issued_serial_numbers = current_issued
            stock_row.issued = False
            stock_row.save(
                update_fields=[
                    "serial_numbers",
                    "quantity",
                    "issued_serial_numbers",
                    "issued",
                ]
            )

            remaining.difference_update(restore_set)

        if remaining:
            # Old rows may not contain allocation metadata. Restore those
            # serials into one controlled EVENT RETURN Inventory row.
            fallback_row = cls.get_return_stock_row(
                outward,
                {},
            )
            current_serials = cls.ensure_inventory_serials(fallback_row)
            current_issued = cls.normalize_serials(
                fallback_row.issued_serial_numbers
            )

            for serial in serials_to_restore:
                if serial in remaining and serial not in current_serials:
                    current_serials.append(serial)

            current_issued = [
                serial
                for serial in current_issued
                if serial not in remaining
            ]

            fallback_row.serial_numbers = current_serials
            fallback_row.quantity = len(current_serials)
            fallback_row.issued_serial_numbers = current_issued
            fallback_row.issued = False
            fallback_row.save(
                update_fields=[
                    "serial_numbers",
                    "quantity",
                    "issued_serial_numbers",
                    "issued",
                ]
            )

        return cls.normalize_serials(
            existing_returned + serials_to_restore
        )

    def save_stock_aware_entry(self, serializer):
        validated = serializer.validated_data
        outward_type = str(
            validated.get("outward_type") or "SCRAP"
        ).strip().upper()
        item_type = str(
            validated.get("item_type") or "COMPONENT"
        ).strip().upper()
        quantity = max(
            int(
                validated.get("quantity")
                or validated.get("no_of_components")
                or 1
            ),
            1,
        )

        save_values = {
            "approval_status": (
                "PENDING_MANAGER"
                if outward_type == "SCRAP"
                else "NOT_REQUESTED"
            ),
            "quantity": quantity,
            "no_of_components": quantity,
            "returned_quantity": 0,
            "returned_serial_numbers": [],
            "stock_restored": False,
        }

        component = validated.get("component")

        if (
            outward_type in {"SALES", "EVENT"}
            and item_type == "COMPONENT"
        ):
            selected_serials = self.normalize_serials(
                validated.get("serial_numbers")
            )
            serials, allocations = self.deduct_component_stock(
                component_id=component.id,
                quantity=quantity,
                selected_serials=selected_serials,
            )

            component_code = str(
                getattr(component, "component_id", "") or ""
            ).strip()
            component_name = str(
                getattr(component, "name", "") or ""
            ).strip()
            component_label = " - ".join(
                value
                for value in [component_code, component_name]
                if value
            )

            save_values.update(
                {
                    "product_name": (
                        validated.get("product_name")
                        or component_label
                        or component_name
                        or component_code
                    ),
                    "serial_numbers": serials,
                    "inventory_allocations": allocations,
                    "stock_deducted": True,
                    "status": (
                        "SOLD"
                        if outward_type == "SALES"
                        else "EVENT_OUT"
                    ),
                }
            )
        else:
            product_name = str(
                validated.get("product_name")
                or validated.get("drone_name")
                or ""
            ).strip()
            save_values.update(
                {
                    "product_name": product_name,
                    "drone_name": (
                        product_name
                        if item_type == "DRONE"
                        else validated.get("drone_name")
                    ),

                    # IMPORTANT FOR ENGINEER MR SCRAP:
                    # Preserve the exact selected issued serial number(s).
                    #
                    # Previously every SCRAP row reached this branch and
                    # serial_numbers was forcibly overwritten with [].
                    # That caused the Scrap Component Details popup to show
                    # "No serial number linked".
                    "serial_numbers": (
                        self.normalize_serials(
                            validated.get(
                                "serial_numbers"
                            )
                        )
                        if outward_type == "SCRAP"
                        else []
                    ),

                    "inventory_allocations": [],
                    "stock_deducted": False,
                    "status": (
                        "SOLD"
                        if outward_type == "SALES"
                        else "EVENT_OUT"
                        if outward_type == "EVENT"
                        else "PENDING_MANAGER"
                        if outward_type == "SCRAP"
                        else validated.get("status") or "NEW"
                    ),
                }
            )

        return serializer.save(**save_values)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            instance = self.save_stock_aware_entry(
                serializer
            )

            self.register_new_scrap_workflow(
                instance,
                getattr(
                    request,
                    "user",
                    None,
                ),
            )

        output = self.get_serializer(instance)
        headers = self.get_success_headers(output.data)
        return Response(
            output.data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="bulk-create",
    )
    def bulk_create(self, request):
        items = request.data.get("items")

        if not isinstance(items, list) or not items:
            raise ValidationError(
                {"items": "Add at least one Component or Drone."}
            )

        common = {
            key: value
            for key, value in request.data.items()
            if key != "items"
        }

        created = []

        with transaction.atomic():
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValidationError(
                        {"items": {str(index): "Invalid item."}}
                    )

                payload = {**common, **item}
                serializer = self.get_serializer(data=payload)

                try:
                    serializer.is_valid(raise_exception=True)
                    created_instance = (
                        self.save_stock_aware_entry(
                            serializer
                        )
                    )

                    self.register_new_scrap_workflow(
                        created_instance,
                        getattr(
                            request,
                            "user",
                            None,
                        ),
                    )

                    created.append(
                        created_instance
                    )
                except ValidationError as error:
                    raise ValidationError(
                        {"items": {str(index): error.detail}}
                    ) from error

        return Response(
            self.get_serializer(created, many=True).data,
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        unlocked = self.get_object()

        with transaction.atomic():
            instance = (
                self.get_queryset()
                .select_for_update()
                .get(pk=unlocked.pk)
            )
            if (
                str(
                    instance.outward_type
                    or ""
                ).strip().upper()
                == "SCRAP"
            ):
                protected_scrap_fields = {
                    "status",
                    "approval_status",
                    "rejection_reason",
                    "rejected_by",

                    # Engineer Scrap staging is changed only by
                    # dedicated backend actions.
                    "source",
                    "scrap_origin",
                    "material_request",
                    "requested_by",
                    "requested_by_user_id",
                    "moved_to_inventory",
                    "moved_at",
                }

                attempted_scrap_fields = (
                    protected_scrap_fields.intersection(
                        request.data.keys()
                    )
                )

                if attempted_scrap_fields:
                    raise ValidationError(
                        {
                            "detail": (
                                "Scrap approval fields cannot be "
                                "changed with normal PATCH. Use "
                                "finance-approve, finance-reject, manager-approve or manager-reject."
                            )
                        }
                    )

            serializer = self.get_serializer(
                instance,
                data=request.data,
                partial=partial,
            )
            serializer.is_valid(
                raise_exception=True
            )

            if instance.stock_deducted:
                # Protect completed stock movement fields, but allow PATCH
                # requests that only update Event return/date information.
                # Earlier serializer logic populated item_type,
                # outward_type and quantity even when they were absent from
                # the PATCH body, causing valid Event returns to be rejected.
                protected_aliases = {
                    "component": {"component"},
                    "item_type": {"item_type", "itemType"},
                    "outward_type": {"outward_type", "typeOfOutward"},
                    "quantity": {
                        "quantity",
                        "no_of_components",
                        "noOfComponents",
                    },
                    "serial_numbers": {
                        "serial_numbers",
                        "serialNumbers",
                    },
                }

                attempted = set()

                for field_name, aliases in protected_aliases.items():
                    if not any(alias in request.data for alias in aliases):
                        continue

                    incoming = serializer.validated_data.get(field_name)

                    if field_name == "component":
                        incoming_value = getattr(incoming, "pk", incoming)
                        current_value = instance.component_id
                    elif field_name in {"item_type", "outward_type"}:
                        incoming_value = str(incoming or "").strip().upper()
                        current_value = str(
                            getattr(instance, field_name, "") or ""
                        ).strip().upper()
                    elif field_name == "quantity":
                        incoming_value = int(incoming or 0)
                        current_value = int(instance.quantity or 0)
                    else:
                        incoming_value = self.normalize_serials(incoming)
                        current_value = self.normalize_serials(
                            instance.serial_numbers
                        )

                    if incoming_value != current_value:
                        attempted.add(field_name)

                if attempted:
                    raise ValidationError(
                        {
                            "detail": (
                                "A completed stock movement cannot change: "
                                + ", ".join(sorted(attempted))
                            )
                        }
                    )

            # Preserve the existing approval state.
            # Sales/Event remain NOT_REQUESTED from creation.
            # Scrap remains REQUESTED until the Manager action.
            save_values = {}

            is_event = str(instance.outward_type).upper() == "EVENT"
            is_component = str(instance.item_type).upper() == "COMPONENT"
            has_return_action = any(
                key in request.data
                for key in [
                    "returned_quantity",
                    "returnedQuantity",
                    "is_returned",
                    "isReturned",
                    "event_components",
                    "eventComponents",
                ]
            )

            if is_event and is_component and has_return_action:
                raw_target = request.data.get(
                    "returned_quantity",
                    request.data.get(
                        "returnedQuantity",
                        instance.returned_quantity,
                    ),
                )

                return_processed = bool(
                    request.data.get(
                        "is_returned",
                        request.data.get(
                            "isReturned",
                            instance.is_returned,
                        ),
                    )
                )

                # Compatibility with an old full-return PATCH.
                if (
                    "returned_quantity" not in request.data
                    and "returnedQuantity" not in request.data
                    and return_processed
                ):
                    raw_target = instance.quantity

                try:
                    target_returned = int(raw_target or 0)
                except (TypeError, ValueError) as error:
                    raise ValidationError(
                        {
                            "returned_quantity": (
                                "Returned quantity must be a whole number."
                            )
                        }
                    ) from error

                returned_serials = self.restore_event_component_stock(
                    instance,
                    target_returned,
                )

                if target_returned >= int(instance.quantity or 0):
                    movement_status = "RETURNED"
                    return_processed = True
                elif return_processed and target_returned > 0:
                    movement_status = "PARTIALLY_RETURNED"
                elif return_processed:
                    movement_status = "CLOSED_NOT_RETURNED"
                elif target_returned > 0:
                    movement_status = "PARTIALLY_RETURNED"
                else:
                    movement_status = "EVENT_OUT"

                save_values.update(
                    {
                        "returned_quantity": target_returned,
                        "returned_serial_numbers": returned_serials,
                        "stock_restored": (
                            target_returned
                            >= int(instance.quantity or 0)
                        ),
                        "is_returned": return_processed,
                        "status": movement_status,
                    }
                )

            elif is_event and has_return_action:
                return_processed = bool(
                    request.data.get(
                        "is_returned",
                        request.data.get(
                            "isReturned",
                            instance.is_returned,
                        ),
                    )
                )
                save_values.update(
                    {
                        "is_returned": return_processed,
                        "status": (
                            "RETURNED"
                            if return_processed
                            else "EVENT_OUT"
                        ),
                    }
                )

            instance = serializer.save(**save_values)

        return Response(self.get_serializer(instance).data)


    @action(
        detail=False,
        methods=["get"],
        url_path="engineer-scrap-options",
    )
    def engineer_scrap_options(self, request):
        user = self.require_engineer_or_admin(request)
        return Response(
            {"material_requests": self.build_engineer_scrap_mr_options(user)},
            status=status.HTTP_200_OK,
        )


    @action(
        detail=False,
        methods=[
            "get",
            "post",
        ],
        url_path="engineer-scrap",
    )
    def engineer_scrap(
        self,
        request,
    ):
        """
        Engineer Scrap flow.

        MR source supports the new disposition:
        - ONLY INVENTORY_ISSUED MR
        - PARTIAL Scrap + Reordering YES/NO
        - TOTAL Scrap
        - multi-component / multi-serial Scrap in one approval request

        Manager -> Finance approval remains unchanged.
        """
        user = self.require_engineer_or_admin(
            request
        )

        if request.method == "GET":
            queryset = (
                OutwardEntry.objects
                .select_related(
                    "component",
                    "material_request",
                )
                .filter(
                    source="ENGINEER",
                    outward_type="SCRAP",
                )
                .order_by(
                    "-out_date",
                    "-created_at",
                    "-id",
                )
            )

            if not self.is_admin_user(
                user
            ):
                queryset = queryset.filter(
                    requested_by_user_id=(
                        user.pk
                    )
                )

            return Response(
                self.get_serializer(
                    queryset,
                    many=True,
                ).data,
                status=status.HTTP_200_OK,
            )

        out_date = (
            request.data.get("out_date")
            or request.data.get("outDate")
            or request.data.get("date")
        )

        if not out_date:
            raise ValidationError(
                {
                    "out_date":
                        "Select the Scrap date."
                }
            )

        remarks = str(
            request.data.get(
                "remarks",
                request.data.get(
                    "reason",
                    "",
                ),
            )
            or ""
        ).strip()

        if not remarks:
            raise ValidationError(
                {
                    "remarks":
                        "Enter Scrap remarks."
                }
            )

        scrap_origin = str(
            request.data.get(
                "scrap_origin",
                request.data.get(
                    "scrapOrigin",
                    "OTHER",
                ),
            )
            or "OTHER"
        ).strip().upper()

        if scrap_origin not in {
            "MR",
            "OTHER",
        }:
            raise ValidationError(
                {
                    "scrap_origin":
                        "Scrap source must be MR or OTHER."
                }
            )

        mr_reference = (
            request.data.get(
                "material_request"
            )
            or request.data.get(
                "material_request_id"
            )
            or request.data.get(
                "materialRequestId"
            )
            or request.data.get(
                "mr_id"
            )
        )

        actor_name = self.get_actor_name(
            user
        )

        with transaction.atomic():
            material_request = None
            component_id = None
            requested_serials = []
            quantity = 0
            product_name = ""
            workflow_metadata = []

            if scrap_origin == "MR":
                if not mr_reference:
                    raise ValidationError(
                        {
                            "material_request":
                                "Select a Material Request."
                        }
                    )

                material_request = (
                    self.get_material_request_from_reference(
                        mr_reference,
                        lock=True,
                    )
                )

                if material_request is None:
                    raise ValidationError(
                        {
                            "material_request":
                                "Material Request was not found."
                        }
                    )

                if (
                    str(
                        material_request.status
                        or ""
                    )
                    .strip()
                    .upper()
                    != "INVENTORY_ISSUED"
                ):
                    raise ValidationError(
                        {
                            "material_request": (
                                "Only an INVENTORY_ISSUED "
                                "Material Request can be "
                                "used for Engineer Scrap."
                            )
                        }
                    )

                component_snapshot = (
                    self.get_engineer_scrap_component_snapshot(
                        material_request,
                        lock=True,
                    )
                )

                if not component_snapshot:
                    raise ValidationError(
                        {
                            "material_request": (
                                "This INVENTORY_ISSUED MR "
                                "has no issued serials "
                                "available for Scrap."
                            )
                        }
                    )

                snapshot_by_component = {
                    str(
                        item["component"]
                    ): item
                    for item in component_snapshot
                }

                scrap_mode = str(
                    request.data.get(
                        "scrap_mode",
                        request.data.get(
                            "scrapMode",
                            "PARTIAL",
                        ),
                    )
                    or "PARTIAL"
                ).strip().upper()

                if scrap_mode not in {
                    "PARTIAL",
                    "TOTAL",
                }:
                    raise ValidationError(
                        {
                            "scrap_mode":
                                "Scrap type must be PARTIAL or TOTAL."
                        }
                    )

                reorder_choice = str(
                    request.data.get(
                        "reorder_choice",
                        request.data.get(
                            "reorderChoice",
                            "",
                        ),
                    )
                    or ""
                ).strip().upper()

                if scrap_mode == "PARTIAL":
                    if reorder_choice not in {
                        "YES",
                        "NO",
                    }:
                        raise ValidationError(
                            {
                                "reorder_choice": (
                                    "Select Reordering "
                                    "Yes or No."
                                )
                            }
                        )
                else:
                    reorder_choice = "NONE"

                raw_scrap_items = (
                    request.data.get(
                        "scrap_items"
                    )
                )

                scrap_items = (
                    self.normalize_scrap_items(
                        raw_scrap_items
                    )
                )

                # Backward compatibility with old one-component UI.
                if (
                    not scrap_items
                    and request.data.get(
                        "component"
                    )
                ):
                    fallback_serials = (
                        self.normalize_serials(
                            request.data.get(
                                "serial_numbers"
                            )
                            or request.data.get(
                                "selected_serials"
                            )
                            or []
                        )
                    )

                    if fallback_serials:
                        scrap_items = [
                            {
                                "component":
                                    request.data.get(
                                        "component"
                                    ),
                                "serial_numbers":
                                    fallback_serials,
                                "quantity":
                                    len(
                                        fallback_serials
                                    ),
                            }
                        ]

                if scrap_mode == "TOTAL":
                    scrap_items = [
                        {
                            "component":
                                item["component"],
                            "serial_numbers":
                                list(
                                    item[
                                        "available_serials"
                                    ]
                                ),
                            "quantity":
                                len(
                                    item[
                                        "available_serials"
                                    ]
                                ),
                        }
                        for item in component_snapshot
                        if item.get(
                            "available_serials"
                        )
                    ]

                if not scrap_items:
                    raise ValidationError(
                        {
                            "serial_numbers":
                                "Select at least one issued serial number."
                        }
                    )

                validated_scrap_items = []
                selected_serial_set = set()

                for raw_item in scrap_items:
                    key = str(
                        raw_item.get(
                            "component"
                        )
                    )

                    snapshot = (
                        snapshot_by_component
                        .get(key)
                    )

                    if snapshot is None:
                        raise ValidationError(
                            {
                                "component": (
                                    "One selected component "
                                    "does not belong to this "
                                    "INVENTORY_ISSUED MR."
                                )
                            }
                        )

                    serials = (
                        self.normalize_serials(
                            raw_item.get(
                                "serial_numbers"
                            )
                            or []
                        )
                    )

                    available = set(
                        snapshot.get(
                            "available_serials",
                            [],
                        )
                        or []
                    )

                    invalid = [
                        serial
                        for serial in serials
                        if serial not in available
                    ]

                    if invalid:
                        raise ValidationError(
                            {
                                "serial_numbers": (
                                    "One or more selected "
                                    "serial numbers are "
                                    "unavailable: "
                                    + ", ".join(
                                        invalid
                                    )
                                )
                            }
                        )

                    for serial in serials:
                        if serial in selected_serial_set:
                            raise ValidationError(
                                {
                                    "serial_numbers":
                                        f"Duplicate selected serial: {serial}"
                                }
                            )

                        selected_serial_set.add(
                            serial
                        )

                    if not serials:
                        continue

                    validated_scrap_items.append(
                        {
                            "component":
                                snapshot[
                                    "component"
                                ],
                            "component_code":
                                snapshot[
                                    "component_code"
                                ],
                            "component_name":
                                snapshot[
                                    "component_name"
                                ],
                            "label":
                                snapshot[
                                    "label"
                                ],
                            "serial_numbers":
                                serials,
                            "quantity":
                                len(serials),
                        }
                    )

                if not validated_scrap_items:
                    raise ValidationError(
                        {
                            "serial_numbers":
                                "Select at least one issued serial number."
                        }
                    )

                all_available_items = [
                    {
                        "component":
                            item[
                                "component"
                            ],
                        "component_code":
                            item[
                                "component_code"
                            ],
                        "component_name":
                            item[
                                "component_name"
                            ],
                        "label":
                            item[
                                "label"
                            ],
                        "serial_numbers":
                            list(
                                item[
                                    "available_serials"
                                ]
                            ),
                        "quantity":
                            len(
                                item[
                                    "available_serials"
                                ]
                            ),
                    }
                    for item in component_snapshot
                ]

                scrap_by_component = {
                    str(
                        item["component"]
                    ): set(
                        item[
                            "serial_numbers"
                        ]
                    )
                    for item in validated_scrap_items
                }

                # -------------------------------------------------
                # FINAL Scrap disposition rule
                # -------------------------------------------------
                # PARTIAL:
                #   SELECTED serials are the usable/action serials.
                #
                #   Reordering YES:
                #       selected   -> NEW MR
                #       unselected -> SCRAP
                #
                #   Reordering NO:
                #       selected   -> INVENTORY / IN STORE
                #       unselected -> SCRAP
                #
                # TOTAL:
                #       all available serials -> SCRAP
                # -------------------------------------------------

                selected_items = (
                    validated_scrap_items
                )

                selected_by_component = {
                    str(
                        item["component"]
                    ): set(
                        item[
                            "serial_numbers"
                        ]
                    )
                    for item in selected_items
                }

                scrap_items = []
                return_items = []
                reorder_items = []

                if scrap_mode == "TOTAL":
                    scrap_items = (
                        all_available_items
                    )
                else:
                    # Unselected serials are ALWAYS Scrap.
                    for item in all_available_items:
                        selected_serials = (
                            selected_by_component
                            .get(
                                str(
                                    item[
                                        "component"
                                    ]
                                ),
                                set(),
                            )
                        )

                        unselected_serials = [
                            serial
                            for serial in item[
                                "serial_numbers"
                            ]
                            if serial
                            not in selected_serials
                        ]

                        if unselected_serials:
                            scrap_items.append(
                                {
                                    "component":
                                        item[
                                            "component"
                                        ],
                                    "component_code":
                                        item[
                                            "component_code"
                                        ],
                                    "component_name":
                                        item[
                                            "component_name"
                                        ],
                                    "label":
                                        item[
                                            "label"
                                        ],
                                    "serial_numbers":
                                        unselected_serials,
                                    "quantity":
                                        len(
                                            unselected_serials
                                        ),
                                }
                            )

                    if reorder_choice == "YES":
                        # Selected serial quantities recreate a NEW MR.
                        reorder_items = [
                            {
                                **item,
                                "serial_numbers":
                                    list(
                                        item[
                                            "serial_numbers"
                                        ]
                                    ),
                                "quantity":
                                    len(
                                        item[
                                            "serial_numbers"
                                        ]
                                    ),
                            }
                            for item in selected_items
                        ]
                    elif reorder_choice == "NO":
                        # Selected serials return to central Inventory.
                        return_items = [
                            {
                                **item,
                                "serial_numbers":
                                    list(
                                        item[
                                            "serial_numbers"
                                        ]
                                    ),
                                "quantity":
                                    len(
                                        item[
                                            "serial_numbers"
                                        ]
                                    ),
                            }
                            for item in selected_items
                        ]

                requested_serials = [
                    serial
                    for item in scrap_items
                    for serial in (
                        item.get(
                            "serial_numbers",
                            [],
                        )
                        or []
                    )
                ]

                quantity = len(
                    requested_serials
                )

                if quantity <= 0:
                    raise ValidationError(
                        {
                            "quantity":
                                "Scrap quantity must be greater than zero."
                        }
                    )

                unique_component_ids = {
                    str(
                        item["component"]
                    )
                    for item
                    in scrap_items
                }

                if (
                    len(
                        unique_component_ids
                    )
                    == 1
                    and scrap_items
                ):
                    component_id = (
                        scrap_items[
                            0
                        ]["component"]
                    )

                    product_name = (
                        scrap_items[
                            0
                        ]["label"]
                    )
                else:
                    component_id = None
                    product_name = (
                        f"{quantity} Scrap item(s) "
                        f"from "
                        f"{material_request.material_request_id}"
                    )

                workflow_metadata = {
                    "workflow":
                        "ENGINEER_MR_SCRAP_DISPOSITION_V1",
                    "scrap_mode":
                        scrap_mode,
                    "reorder_choice":
                        reorder_choice,
                    "source_mr_id":
                        material_request.id,
                    "source_mr_number":
                        material_request
                        .material_request_id,
                    # Selected serials are the Engineer's usable/action selection.
                    "selected_items":
                        selected_items,

                    # Unselected serials are the actual Scrap items.
                    "scrap_items":
                        scrap_items,

                    # Selected serials returned to Inventory when Reordering = NO.
                    "return_items":
                        return_items,

                    # Selected serial quantities used to create the NEW MR when
                    # Reordering = YES.
                    "reorder_items":
                        reorder_items,

                    "all_available_items":
                        all_available_items,

                    "selected_quantity":
                        sum(
                            item[
                                "quantity"
                            ]
                            for item
                            in selected_items
                        ),

                    "scrap_quantity":
                        quantity,

                    "return_quantity":
                        sum(
                            item[
                                "quantity"
                            ]
                            for item
                            in return_items
                        ),

                    "reorder_quantity":
                        sum(
                            item[
                                "quantity"
                            ]
                            for item
                            in reorder_items
                        ),
                    "disposition_processed":
                        False,
                    "replacement_mr_id":
                        None,
                    "replacement_mr_number":
                        "",
                    "returned_inventory_ids":
                        [],
                }
            else:
                component_id = (
                    request.data.get(
                        "component"
                    )
                    or request.data.get(
                        "component_id"
                    )
                )

                if not component_id:
                    raise ValidationError(
                        {
                            "component":
                                "Select a component."
                        }
                    )

                try:
                    quantity = int(
                        request.data.get(
                            "quantity",
                            request.data.get(
                                "qty",
                                1,
                            ),
                        )
                        or 0
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    quantity = 0

                if quantity <= 0:
                    raise ValidationError(
                        {
                            "quantity":
                                "Quantity must be greater than zero."
                        }
                    )

                requested_serials = []

                product_name = str(
                    request.data.get(
                        "product_name",
                        request.data.get(
                            "productName",
                            "",
                        ),
                    )
                    or ""
                ).strip()

            payload = {
                "outward_type":
                    "SCRAP",
                "item_type":
                    "COMPONENT",
                "out_date":
                    out_date,
                "component":
                    component_id,
                "quantity":
                    quantity,
                "serial_numbers":
                    requested_serials,
                "remarks":
                    remarks,
                "product_name":
                    product_name,
            }

            serializer = self.get_serializer(
                data=payload
            )

            serializer.is_valid(
                raise_exception=True
            )

            instance = (
                self.save_stock_aware_entry(
                    serializer
                )
            )

            component = instance.component

            if (
                component is not None
                and not str(
                    instance.product_name
                    or ""
                ).strip()
            ):
                code = str(
                    getattr(
                        component,
                        "component_id",
                        "",
                    )
                    or ""
                ).strip()

                name = str(
                    getattr(
                        component,
                        "name",
                        "",
                    )
                    or ""
                ).strip()

                instance.product_name = (
                    " - ".join(
                        value
                        for value in [
                            code,
                            name,
                        ]
                        if value
                    )
                    or name
                    or code
                )

            instance.source = "ENGINEER"
            instance.scrap_origin = (
                scrap_origin
            )
            instance.material_request = (
                material_request
            )
            instance.requested_by = (
                actor_name
            )
            instance.requested_by_user_id = (
                user.pk
            )
            instance.moved_to_inventory = (
                False
            )
            instance.moved_at = None
            instance.approval_status = (
                "PENDING_MANAGER"
            )
            instance.status = (
                "PENDING_MANAGER"
            )

            if (
                scrap_origin == "MR"
                and isinstance(
                    workflow_metadata,
                    dict,
                )
            ):
                instance.inventory_allocations = (
                    workflow_metadata
                )

            instance.save(
                update_fields=[
                    "product_name",
                    "serial_numbers",
                    "inventory_allocations",
                    "source",
                    "scrap_origin",
                    "material_request",
                    "requested_by",
                    "requested_by_user_id",
                    "moved_to_inventory",
                    "moved_at",
                    "approval_status",
                    "status",
                    "updated_at",
                ]
            )

            # Existing Manager -> Finance Scrap notification flow is unchanged.
            self.register_new_scrap_workflow(
                instance,
                user,
            )

        return Response(
            self.get_serializer(
                instance
            ).data,
            status=status.HTTP_201_CREATED,
        )


    @action(
        detail=False,
        methods=["post"],
        url_path="engineer-scrap-bulk-delete",
    )
    def engineer_scrap_bulk_delete(
        self,
        request,
    ):
        """
        Group Delete / remove used by the Engineer Scrap page.

        Behavior:
        - Pending / Manager Approved / Rejected Engineer Scrap:
          delete the staging OutwardEntry and its Manager notification.

        - Moved-to-Inventory Engineer Scrap:
          DO NOT delete the OutwardEntry because Inventory and Outward
          already use that same row. Instead change source to DIRECT,
          which removes it only from GET /outward/engineer-scrap/
          while keeping it visible in normal Inventory/Outward Scrap.

        Safety:
        - Engineer/Admin only.
        - Engineer can remove only own Engineer Scrap records.
        - Existing DIRECT Inventory/Outward Scrap cannot be selected here.
        - Entire operation is atomic.
        """
        user = (
            self.require_engineer_or_admin(
                request
            )
        )

        raw_ids = (
            request.data.get("ids")
            or []
        )

        if not isinstance(
            raw_ids,
            list,
        ):
            raise ValidationError(
                {
                    "ids":
                        "ids must be a list."
                }
            )

        cleaned_ids = []

        for raw_id in raw_ids:
            try:
                value = int(raw_id)
            except (
                TypeError,
                ValueError,
            ):
                continue

            if (
                value > 0
                and value
                not in cleaned_ids
            ):
                cleaned_ids.append(
                    value
                )

        if not cleaned_ids:
            raise ValidationError(
                {
                    "ids":
                        "Select at least one Engineer Scrap record."
                }
            )

        with transaction.atomic():
            queryset = (
                OutwardEntry.objects
                .select_for_update()
                .filter(
                    pk__in=cleaned_ids,
                    source="ENGINEER",
                    outward_type="SCRAP",
                )
            )

            selected_rows = list(
                queryset
            )

            found_ids = {
                int(row.pk)
                for row
                in selected_rows
            }

            missing_ids = [
                value
                for value
                in cleaned_ids
                if value
                not in found_ids
            ]

            if missing_ids:
                raise ValidationError(
                    {
                        "detail": (
                            "One or more selected records are not "
                            "Engineer Scrap staging records."
                        ),
                        "ids":
                            missing_ids,
                    }
                )

            if (
                not self.is_admin_user(
                    user
                )
            ):
                unauthorized_ids = [
                    int(row.pk)
                    for row
                    in selected_rows
                    if (
                        row.requested_by_user_id
                        and int(
                            row.requested_by_user_id
                        )
                        != int(user.pk)
                    )
                ]

                if unauthorized_ids:
                    raise PermissionDenied(
                        "You can delete only your own Engineer Scrap requests."
                    )

            moved_ids = [
                int(row.pk)
                for row
                in selected_rows
                if row.moved_to_inventory
            ]

            staging_ids = [
                int(row.pk)
                for row
                in selected_rows
                if not row.moved_to_inventory
            ]

            # -------------------------------------------------
            # 1. NOT YET MOVED:
            #    delete the Engineer Scrap staging record.
            #    Its Finance/Manager notification would otherwise point
            #    to a deleted Scrap request, so remove it too.
            # -------------------------------------------------
            if staging_ids:
                Notification.objects.filter(
                    category="SCRAP",
                    receiver__in=["FINANCE", "MANAGER"],
                    reference_id__in=[
                        str(value)
                        for value
                        in staging_ids
                    ],
                ).delete()

                OutwardEntry.objects.filter(
                    pk__in=staging_ids,
                    source="ENGINEER",
                    outward_type="SCRAP",
                    moved_to_inventory=False,
                ).delete()

            # -------------------------------------------------
            # 2. ALREADY MOVED:
            #    remove ONLY from Engineer Scrap page.
            #
            #    /outward/engineer-scrap/ filters source=ENGINEER,
            #    while normal Inventory/Outward accepts DIRECT.
            #    Therefore changing source preserves the real Scrap
            #    record in Inventory and Outward.
            # -------------------------------------------------
            if moved_ids:
                OutwardEntry.objects.filter(
                    pk__in=moved_ids,
                    source="ENGINEER",
                    outward_type="SCRAP",
                    moved_to_inventory=True,
                ).update(
                    source="DIRECT",
                )

            removed_ids = [
                int(row.pk)
                for row
                in selected_rows
            ]

        return Response(
            {
                "removed_count":
                    len(removed_ids),

                # Backward-compatible key for the current frontend.
                "deleted_count":
                    len(removed_ids),

                "removed_ids":
                    removed_ids,

                "deleted_staging_ids":
                    staging_ids,

                "removed_from_engineer_page_only_ids":
                    moved_ids,
            },
            status=status.HTTP_200_OK,
        )


    @action(
        detail=True,
        methods=["delete"],
        url_path="engineer-scrap-delete",
    )
    def engineer_scrap_delete(
        self,
        request,
        pk=None,
    ):
        """
        Delete ONLY an Engineer Scrap staging request.

        Safety rules:
        - Engineer/Admin only.
        - Engineer can delete only their own request.
        - DIRECT Inventory/Outward Scrap cannot use this action.
        - Once moved_to_inventory=True, deletion is blocked because
          the row is now the official Inventory/Outward Scrap record.
        - Any matching Finance/Manager Scrap notification is removed too.
        """
        user = self.require_engineer_or_admin(
            request
        )

        with transaction.atomic():
            instance = (
                OutwardEntry.objects
                .select_for_update()
                .get(pk=pk)
            )

            if (
                str(
                    instance.source
                    or ""
                ).strip().upper()
                != "ENGINEER"
                or str(
                    instance.outward_type
                    or ""
                ).strip().upper()
                != "SCRAP"
            ):
                raise ValidationError(
                    {
                        "detail": (
                            "Only Engineer Scrap staging records "
                            "can be deleted from the Engineer Scrap page."
                        )
                    }
                )

            if (
                not self.is_admin_user(
                    user
                )
                and instance.requested_by_user_id
                and int(
                    instance.requested_by_user_id
                ) != int(user.pk)
            ):
                raise PermissionDenied(
                    "You can delete only your own Engineer Scrap request."
                )

            if (
                instance.moved_to_inventory
            ):
                raise ValidationError(
                    {
                        "detail": (
                            "This Scrap has already been moved to Inventory. "
                            "It cannot be deleted from the Engineer Scrap page."
                        )
                    }
                )

            reference_id = str(
                instance.pk
            )

            # Remove only the notification belonging to this Scrap request.
            Notification.objects.filter(
                category="SCRAP",
                reference_id=reference_id,
                receiver__in=["FINANCE", "MANAGER"],
            ).delete()

            # Delete only this staged Engineer Scrap row.
            instance.delete()

        return Response(
            status=status.HTTP_204_NO_CONTENT,
        )


    @action(
        detail=True,
        methods=["post"],
        url_path="move-to-inventory",
    )
    def move_to_inventory(
        self,
        request,
        pk=None,
    ):
        """
        Engineer/Admin may perform this only AFTER final Finance approval.

        We do not create a duplicate OutwardEntry.

        Instead, the same staged record becomes list-visible.
        Inventory and Outward already load GET /outward/, so the row
        automatically appears in both Scrap tables after this action.
        """
        user = self.require_engineer_or_admin(
            request
        )

        with transaction.atomic():
            instance = (
                self.get_queryset()
                .select_for_update()
                .get(pk=pk)
            )

            if (
                str(
                    instance.source
                    or ""
                ).strip().upper()
                != "ENGINEER"
            ):
                raise ValidationError(
                    {
                        "detail": (
                            "Only Engineer-raised Scrap "
                            "can use Move to Inventory."
                        )
                    }
                )

            if (
                not self.is_admin_user(
                    user
                )
                and instance.requested_by_user_id
                and int(
                    instance.requested_by_user_id
                ) != int(user.pk)
            ):
                raise PermissionDenied(
                    "You can move only your own Engineer Scrap request."
                )

            if (
                instance.moved_to_inventory
            ):
                # Idempotent: repeated click returns the already-moved row.
                return Response(
                    self.get_serializer(
                        instance
                    ).data,
                    status=status.HTTP_200_OK,
                )

            current = str(
                instance.approval_status
                or ""
            ).strip().upper()

            if (
                current
                != "APPROVED"
            ):
                raise ValidationError(
                    {
                        "detail": (
                            "Manager and Finance approval are required before "
                            "moving Scrap to Inventory. "
                            f"Current state: {current or 'UNKNOWN'}."
                        )
                    }
                )

            # Execute the new MR Scrap disposition ONLY NOW:
            # Finance has approved and Engineer clicked Move to Store.
            #
            # This means merely opening/submitting the Scrap popup does not
            # move good components, create a reorder MR, or finalize Scrap.
            metadata = (
                self.process_engineer_scrap_disposition(
                    scrap_entry=instance,
                )
            )

            instance.inventory_allocations = (
                metadata
            )

            instance.stock_restored = bool(
                isinstance(
                    metadata,
                    dict,
                )
                and metadata.get(
                    "returned_inventory_ids"
                )
            )

            instance.moved_to_inventory = (
                True
            )
            instance.moved_at = (
                timezone.now()
            )

            # Keep the final Finance-approved audit state after movement.
            instance.approval_status = (
                "APPROVED"
            )
            instance.status = (
                "APPROVED"
            )

            instance.save(
                update_fields=[
                    "inventory_allocations",
                    "stock_restored",
                    "moved_to_inventory",
                    "moved_at",
                    "approval_status",
                    "status",
                    "updated_at",
                ]
            )

        return Response(
            self.get_serializer(
                instance
            ).data,
            status=status.HTTP_200_OK,
        )


    @action(
        detail=True,
        methods=["post"],
        url_path="finance-approve",
    )
    def finance_approve(
        self,
        request,
        pk=None,
    ):
        """
        Finance is the FINAL Scrap approval stage.

        PENDING_FINANCE -> APPROVED
        """
        user = self.require_finance(request)

        with transaction.atomic():
            instance = (
                self.get_queryset()
                .select_for_update()
                .get(pk=pk)
            )

            if (
                str(
                    instance.outward_type
                    or ""
                ).strip().upper()
                != "SCRAP"
            ):
                raise ValidationError(
                    {
                        "detail":
                            "Only Scrap records require Finance approval."
                    }
                )

            current = str(
                instance.approval_status
                or ""
            ).strip().upper()

            if current != "PENDING_FINANCE":
                raise ValidationError(
                    {
                        "detail": (
                            "This Scrap is no longer pending "
                            "Finance approval. Current state: "
                            f"{current or 'UNKNOWN'}."
                        )
                    }
                )

            finance_name = (
                self.get_actor_name(user)
            )

            instance.approval_status = "APPROVED"
            instance.status = "APPROVED"
            instance.rejection_reason = None
            instance.rejected_by = None

            instance.save(
                update_fields=[
                    "approval_status",
                    "status",
                    "rejection_reason",
                    "rejected_by",
                    "updated_at",
                ]
            )

            # Mark the Finance notification as processed.
            Notification.objects.filter(
                category="SCRAP",
                receiver="FINANCE",
                reference_id=str(
                    instance.pk
                ),
            ).update(
                status="APPROVED",
                is_read=True,
                message=(
                    f"Scrap approved by Finance "
                    f"{finance_name}."
                ),
            )

            # Final result goes only to the exact creator.
            self.ensure_scrap_creator_notification(
                instance,
                finance_name,
                actor_role="Finance",
                outcome="approved",
            )

        return Response(
            self.get_serializer(
                instance
            ).data,
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="finance-reject",
    )
    def finance_reject(
        self,
        request,
        pk=None,
    ):
        """
        Finance rejection is final and occurs only after Manager approval.
        """
        user = self.require_finance(request)

        reason = str(
            request.data.get(
                "rejection_reason",
                request.data.get(
                    "reason",
                    "",
                ),
            )
            or ""
        ).strip()

        if not reason:
            raise ValidationError(
                {
                    "rejection_reason":
                        "Enter a rejection reason."
                }
            )

        with transaction.atomic():
            instance = (
                self.get_queryset()
                .select_for_update()
                .get(pk=pk)
            )

            if (
                str(
                    instance.outward_type
                    or ""
                ).strip().upper()
                != "SCRAP"
            ):
                raise ValidationError(
                    {
                        "detail":
                            "Only Scrap records use this Finance workflow."
                    }
                )

            current = str(
                instance.approval_status
                or ""
            ).strip().upper()

            if current != "PENDING_FINANCE":
                raise ValidationError(
                    {
                        "detail": (
                            "This Scrap is no longer pending "
                            "Finance approval. Current state: "
                            f"{current or 'UNKNOWN'}."
                        )
                    }
                )

            finance_name = (
                self.get_actor_name(user)
            )

            instance.approval_status = (
                "FINANCE_REJECTED"
            )
            instance.status = "FINANCE_REJECTED"
            instance.rejection_reason = reason
            instance.rejected_by = finance_name

            instance.save(
                update_fields=[
                    "approval_status",
                    "status",
                    "rejection_reason",
                    "rejected_by",
                    "updated_at",
                ]
            )

            Notification.objects.filter(
                category="SCRAP",
                receiver="FINANCE",
                reference_id=str(
                    instance.pk
                ),
            ).update(
                status="FINANCE_REJECTED",
                is_read=True,
                message=(
                    f"Scrap rejected by Finance "
                    f"{finance_name}. "
                    f"Reason: {reason}"
                ),
            )

            # Manager has already approved at this point, so keep the
            # Manager notification as audit history and notify only the creator
            # of the final Finance rejection.
            self.ensure_scrap_creator_notification(
                instance,
                finance_name,
                actor_role="Finance",
                outcome="rejected",
                rejection_reason=reason,
            )

        return Response(
            self.get_serializer(
                instance
            ).data,
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="manager-approve",
    )
    def manager_approve(
        self,
        request,
        pk=None,
    ):
        """
        Manager is the FIRST Scrap approval stage.

        PENDING_MANAGER -> PENDING_FINANCE
        Then create the Finance notification and Finance approval email.
        """
        user = self.require_manager(request)

        with transaction.atomic():
            instance = (
                self.get_queryset()
                .select_for_update()
                .get(pk=pk)
            )

            if (
                str(
                    instance.outward_type
                    or ""
                ).strip().upper()
                != "SCRAP"
            ):
                raise ValidationError(
                    {
                        "detail":
                            "Only Scrap records require Manager approval."
                    }
                )

            current = str(
                instance.approval_status
                or ""
            ).strip().upper()

            if current != "PENDING_MANAGER":
                raise ValidationError(
                    {
                        "detail": (
                            "This Scrap is no longer pending "
                            f"Manager approval. Current state: "
                            f"{current or 'UNKNOWN'}."
                        )
                    }
                )

            manager_name = (
                self.get_actor_name(user)
            )

            # Manager approval advances the request to Finance.
            instance.approval_status = (
                "PENDING_FINANCE"
            )
            instance.status = "PENDING_FINANCE"
            instance.rejection_reason = None
            instance.rejected_by = None

            instance.save(
                update_fields=[
                    "approval_status",
                    "status",
                    "rejection_reason",
                    "rejected_by",
                    "updated_at",
                ]
            )

            # Manager's own notification is now completed.
            Notification.objects.filter(
                category="SCRAP",
                receiver="MANAGER",
                reference_id=str(
                    instance.pk
                ),
            ).update(
                status="MANAGER_APPROVED",
                is_read=True,
                message=(
                    f"Scrap approved by "
                    f"{manager_name}; pending Finance approval."
                ),
            )

            # Finance notification exists only after Manager approval.
            self.ensure_scrap_finance_notification(
                instance,
                instance.requested_by
                or "User",
            )

            transaction.on_commit(
                lambda outward_id=instance.pk: (
                    self.send_scrap_finance_approval_email(
                        outward_id
                    )
                )
            )

        return Response(
            self.get_serializer(
                instance
            ).data,
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="manager-reject",
    )
    def manager_reject(
        self,
        request,
        pk=None,
    ):
        user = self.require_manager(request)

        reason = str(
            request.data.get(
                "rejection_reason",
                request.data.get(
                    "reason",
                    "",
                ),
            )
            or ""
        ).strip()

        if not reason:
            raise ValidationError(
                {
                    "rejection_reason":
                        "Enter a rejection reason."
                }
            )

        with transaction.atomic():
            instance = (
                self.get_queryset()
                .select_for_update()
                .get(pk=pk)
            )

            if (
                str(
                    instance.outward_type
                    or ""
                ).strip().upper()
                != "SCRAP"
            ):
                raise ValidationError(
                    {
                        "detail":
                            "Only Scrap records use this Manager workflow."
                    }
                )

            current = str(
                instance.approval_status
                or ""
            ).strip().upper()

            if current != "PENDING_MANAGER":
                raise ValidationError(
                    {
                        "detail": (
                            "This Scrap is no longer pending "
                            f"Manager approval. Current state: "
                            f"{current or 'UNKNOWN'}."
                        )
                    }
                )

            instance.approval_status = "MANAGER_REJECTED"
            instance.status = "MANAGER_REJECTED"
            instance.rejection_reason = reason
            instance.rejected_by = (
                self.get_actor_name(user)
            )

            instance.save(
                update_fields=[
                    "approval_status",
                    "status",
                    "rejection_reason",
                    "rejected_by",
                    "updated_at",
                ]
            )

            manager_name = (
                self.get_actor_name(
                    user
                )
            )

            Notification.objects.filter(
                category="SCRAP",
                receiver="MANAGER",
                reference_id=str(
                    instance.pk
                ),
            ).update(
                status="MANAGER_REJECTED",
                is_read=True,
                message=(
                    f"Scrap rejected by "
                    f"{manager_name}. "
                    f"Reason: {reason}"
                ),
            )

            # Manager rejection is final. Remove any impossible stale
            # Finance-stage notification and notify only the exact creator.
            Notification.objects.filter(
                category="SCRAP",
                receiver="FINANCE",
                reference_id=str(
                    instance.pk
                ),
            ).delete()

            self.ensure_scrap_creator_notification(
                instance,
                manager_name,
                actor_role="Manager",
                outcome="rejected",
                rejection_reason=reason,
            )

        return Response(
            self.get_serializer(
                instance
            ).data,
            status=status.HTTP_200_OK,
        )

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        if instance.stock_deducted:
            return Response(
                {
                    "detail": (
                        "This record changed In-Store stock and cannot be "
                        "deleted. Keep it as an audit record."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return super().destroy(request, *args, **kwargs)