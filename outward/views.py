from uuid import uuid4
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from django.core.cache import cache
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from inventory_backend.cache_utils import (
    build_list_cache_key,
    get_cache_version,
    invalidate_cache_version,
)
from inventory_backend.pagination import (
    OptionalPageNumberPagination,
    apply_server_query_parameters,
)
from rest_framework_simplejwt.authentication import JWTAuthentication

from inventory.models import (
    Inventory,
    InventoryReservation,
    ProjectInventory,
    DroneInstance,
    DroneComponentAllocation,
)
from inventory.drone_instances import (
    ensure_drone_instances,
    refresh_drone_instance_statuses,
    normalize_serials as normalize_drone_serials,
)
from materialrequest.models import MaterialRequest, BOMItem, RDItem, RequestItem
from procurement.models import PurchaseOrder, PurchaseOrderApproval, PurchaseOrderItem
from notifications.email_service import send_ipms_email
from notifications.models import Notification

from .models import OutwardEntry
from .serializers import OutwardEntrySerializer


User = get_user_model()

OUTWARD_LIST_CACHE_TTL_SECONDS = 60
OUTWARD_LIST_CACHE_VERSION_KEY = "ipms:outward:list:version"


def get_outward_cache_version():
    return get_cache_version(OUTWARD_LIST_CACHE_VERSION_KEY)


def invalidate_outward_cache():
    invalidate_cache_version(OUTWARD_LIST_CACHE_VERSION_KEY)


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
            "material_request__requester",
        )
        .all()
        .order_by("-out_date", "-created_at", "-id")
    )
    serializer_class = OutwardEntrySerializer
    pagination_class = OptionalPageNumberPagination

    # Parse `Authorization: Bearer <access-token>` for this ViewSet.
    #
    # This is important for manager-approve / manager-reject because
    # those actions use request.user to verify that the caller is
    # actually a Manager.
    authentication_classes = [
        JWTAuthentication,
    ]

    def list(self, request, *args, **kwargs):
        version = get_outward_cache_version()
        cache_key = None

        if version:
            cache_key = build_list_cache_key(
                "ipms:outward:list",
                version,
                request,
            )
            try:
                cached_data = cache.get(cache_key)
            except Exception:
                cached_data = None
            if cached_data is not None:
                return Response(cached_data)

        response = super().list(request, *args, **kwargs)

        if cache_key and response.status_code == 200:
            try:
                cache.set(
                    cache_key,
                    response.data,
                    timeout=OUTWARD_LIST_CACHE_TTL_SECONDS,
                )
            except Exception:
                pass

        return response

    def get_queryset(self):
        """
        Normal Inventory/Outward screens call GET /outward/.

        Engineer-raised Scrap appears after Finance has executed the final
        disposition. New rows persist moved_to_inventory=True; older rows are
        also recognized through disposition_processed metadata.

        Detail/custom actions still see the staged row so Manager
        Notifications can approve/reject it by ID.
        """
        queryset = super().get_queryset()

        if getattr(self, "action", "") == "list":
            queryset = queryset.filter(
                Q(source="DIRECT")
                | Q(
                    source="ENGINEER",
                    moved_to_inventory=True,
                )
                | Q(
                    source="ENGINEER",
                    inventory_allocations__disposition_processed=True,
                )
                # Returned-drone QC failures must be visible immediately in
                # Outward -> Failed QC while Manager / Finance approval is
                # still pending. Other staged Engineer Scrap remains hidden.
                | Q(
                    inventory_allocations__workflow__in=[
                        "RETURNABLE_DRONE_QC_V1",
                        "RETURNABLE_COMPONENT_QC_V1",
                    ],
                )
            )

        from_scrap = str(
            self.request.query_params.get(
                "from_scrap",
                "",
            )
            or ""
        ).strip().lower()

        if from_scrap in {"1", "true", "yes", "on"}:
            queryset = queryset.filter(
                scrap_origin="MR",
            )

        material_request = str(
            self.request.query_params.get(
                "material_request",
                "",
            )
            or ""
        ).strip()

        if material_request:
            if material_request.isdigit():
                queryset = queryset.filter(
                    material_request_id=int(material_request)
                )
            else:
                queryset = queryset.filter(
                    material_request__material_request_id=
                        material_request
                )

        return apply_server_query_parameters(
            queryset,
            self.request,
            search_fields=(
                "code",
                "outward_type",
                "item_type",
                "product_name",
                "component__component_id",
                "component__name",
                "invoice_number",
                "client",
                "gate_pass",
                "source",
            ),
            filter_fields={
                "outward_type": "outward_type__iexact",
                "item_type": "item_type__iexact",
                "source": "source__iexact",
                "approval_status": "approval_status__iexact",
                "component": "component_id",
            },
            ordering_fields=(
                "code",
                "out_date",
                "quantity",
                "outward_type",
                "created_at",
            ),
            default_ordering=("-out_date", "-created_at", "-id"),
        )

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
        """
        Authorize Scrap Manager actions using the JWT active_role.

        A user can have multiple assigned roles in IPMS. The role selected
        for the current login/session is stored in the JWT as active_role.
        Checking only request.user.role incorrectly rejects a valid Manager
        session when Manager is an additional/selected role.
        """
        user = getattr(request, "user", None)

        if (
            not user
            or not user.is_authenticated
        ):
            raise PermissionDenied(
                "Authentication is required."
            )

        active_role = cls.get_active_role(request)

        if (
            active_role not in {
                "manager",
                "admin",
            }
            and not getattr(
                user,
                "is_superuser",
                False,
            )
        ):
            raise PermissionDenied(
                "Only Manager can approve or reject Scrap."
            )

        return user

    @classmethod
    def require_finance(cls, request):
        """
        Authorize the Finance Scrap stage using the current JWT active_role.
        """
        user = getattr(request, "user", None)

        if (
            not user
            or not user.is_authenticated
        ):
            raise PermissionDenied(
                "Authentication is required."
            )

        active_role = cls.get_active_role(request)

        if (
            active_role not in {
                "finance",
                "admin",
            }
            and not getattr(
                user,
                "is_superuser",
                False,
            )
        ):
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

        role = cls.get_active_role(request)

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
    def get_scrap_requester_display_name(
        cls,
        instance,
        authenticated_user=None,
    ):
        """
        Return a human-readable requester name for Scrap notifications.

        Priority:
        1. Original MR requester employee/display name.
        2. Original MR requester_name.
        3. Authenticated Scrap creator employee/display name.
        4. Legacy username/email fallback.

        Never expose the full email address in Manager/Finance Scrap UI.
        """
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

        candidates = []

        if mr_requester is not None:
            mr_full_name = ""

            try:
                mr_full_name = (
                    mr_requester.get_full_name()
                    or ""
                )
            except Exception:
                mr_full_name = ""

            candidates.extend(
                [
                    getattr(
                        mr_requester,
                        "employee_name",
                        "",
                    ),
                    getattr(
                        mr_requester,
                        "name",
                        "",
                    ),
                    mr_full_name,
                ]
            )

        if material_request is not None:
            candidates.append(
                getattr(
                    material_request,
                    "requester_name",
                    "",
                )
            )

        if authenticated_user is not None:
            auth_full_name = ""

            try:
                auth_full_name = (
                    authenticated_user
                    .get_full_name()
                    or ""
                )
            except Exception:
                auth_full_name = ""

            candidates.extend(
                [
                    getattr(
                        authenticated_user,
                        "employee_name",
                        "",
                    ),
                    getattr(
                        authenticated_user,
                        "name",
                        "",
                    ),
                    auth_full_name,
                ]
            )

        for value in candidates:
            text = str(
                value or ""
            ).strip()

            if (
                text
                and "@" not in text
                and not (
                    "." in text
                    and " " not in text
                )
            ):
                return text[:50]

        # Legacy/login fallback.
        fallback = ""

        if authenticated_user is not None:
            fallback = str(
                getattr(
                    authenticated_user,
                    "username",
                    "",
                )
                or getattr(
                    authenticated_user,
                    "email",
                    "",
                )
                or ""
            ).strip()

        if not fallback:
            fallback = str(
                getattr(
                    material_request,
                    "requester_name",
                    "",
                )
                if material_request is not None
                else ""
            ).strip()

        if not fallback:
            fallback = str(
                getattr(
                    instance,
                    "requested_by",
                    "",
                )
                or "User"
            ).strip()

        if "@" in fallback:
            fallback = (
                fallback
                .split("@", 1)[0]
                .strip()
            )

        if (
            "." in fallback
            and " " not in fallback
        ):
            fallback = (
                fallback
                .split(".", 1)[0]
                .strip()
            )

        if not fallback:
            fallback = "User"

        return (
            fallback[:1].upper()
            + fallback[1:]
        )[:50]


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
            cls.get_scrap_requester_display_name(
                instance,
                authenticated_user,
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
    def get_engineer_scrap_mr_unavailable_reason(
        cls,
        material_request,
    ):
        """
        Return why this In-Drone MR is NOT eligible for Engineer Scrap.

        Engineer Scrap is allowed only for an idle/free In-Drone MR — the
        same business state in which Inventory can offer the Sale action.

        Block while:
        - Sales is pending/approved (sold or reserved for Sales)
        - Flight Test is active
        - Customer Demo / Trials is active
        - Event is active
        - a returned temporary movement is still waiting for successful QC

        A temporary usage becomes available again only when its return is:
            return_condition = OK
            return_approval_status = COMPLETED

        Rejected Sales/Return workflows do not block the MR.
        """
        if material_request is None:
            return "MATERIAL_REQUEST_MISSING"

        rejected_states = {
            "REJECTED",
            "MANAGER_REJECTED",
            "FINANCE_REJECTED",
            "CANCELLED",
            "CANCELED",
        }

        sales_rows = (
            OutwardEntry.objects
            .filter(
                outward_type="SALES",
                material_request=material_request,
            )
            .only(
                "id",
                "approval_status",
                "status",
            )
        )

        for sales_row in sales_rows:
            approval_state = str(
                sales_row.approval_status or ""
            ).strip().upper()

            status_state = str(
                sales_row.status or ""
            ).strip().upper()

            if (
                approval_state in rejected_states
                or status_state in rejected_states
            ):
                continue

            return "SALES"

        # Keep the import local to avoid introducing a module-level circular
        # dependency in Outward views.
        from componentusage.models import ComponentUsage

        usage_rows = (
            ComponentUsage.objects
            .filter(
                material_request=material_request,
                purpose__in=[
                    "FLIGHT_TEST",
                    "CUSTOMER_DEMO",
                    "EVENT",
                ],
            )
            .only(
                "id",
                "purpose",
                "return_condition",
                "return_approval_status",
                "received_date",
            )
        )

        for usage in usage_rows:
            approval_state = str(
                getattr(
                    usage,
                    "return_approval_status",
                    "",
                )
                or ""
            ).strip().upper()

            if approval_state in rejected_states:
                continue

            condition_state = str(
                getattr(
                    usage,
                    "return_condition",
                    "",
                )
                or ""
            ).strip().upper()

            fully_released = (
                condition_state == "OK"
                and approval_state == "COMPLETED"
            )

            if fully_released:
                continue

            purpose = str(
                getattr(
                    usage,
                    "purpose",
                    "",
                )
                or ""
            ).strip().upper()

            return purpose or "IN_USE"

        return ""

    @classmethod
    def build_engineer_scrap_mr_options(cls, user):
        """
        Engineer Scrap dropdown is physical-drone based.

        Only DroneInstance rows whose current state is AVAILABLE are shown.
        Therefore if MR-X has two drones and _01 is sold/Flight Test/scrapped,
        only MR-X / _02 remains selectable for Engineer Scrap.
        """
        material_requests = list(
            MaterialRequest.objects.filter(
                status__in=cls.get_engineer_in_drone_statuses()
            )
            .exclude(request_type="RETURNABLE")
            .order_by("-date", "-id")[:500]
        )

        options = []
        for material_request in material_requests:
            ensure_drone_instances(material_request)
            instances = refresh_drone_instance_statuses(material_request)

            for drone_instance in instances:
                if str(drone_instance.status or "").strip().upper() != "AVAILABLE":
                    continue

                allocations = list(
                    DroneComponentAllocation.objects.select_related("component")
                    .filter(drone_instance=drone_instance, quantity__gt=0)
                    .order_by("component_id")
                )
                components = []
                for allocation in allocations:
                    serials = cls.normalize_serials(allocation.serial_numbers)
                    if not serials and int(allocation.quantity or 0) <= 0:
                        continue
                    component = allocation.component
                    component_code = str(
                        getattr(component, "component_id", "") or ""
                    ).strip()
                    component_name = str(getattr(component, "name", "") or "").strip()
                    label = " - ".join(
                        value for value in (component_code, component_name) if value
                    ) or component_code or component_name or f"Component {component.pk}"
                    components.append({
                        "component": component.pk,
                        "component_code": component_code,
                        "component_name": component_name,
                        "label": label,
                        "issued_serials": serials,
                        "available_serials": serials,
                        "issued_quantity": int(allocation.quantity or 0),
                        "requested_quantity": int(allocation.quantity or 0),
                        "issue_status": "ISSUED",
                    })

                if not components:
                    continue

                display_id = (
                    f"{material_request.material_request_id} / {drone_instance.suffix}"
                )
                options.append({
                    # Keep base MR id for the existing Scrap form/backend.
                    "id": material_request.id,
                    "option_id": f"{material_request.id}:{drone_instance.id}",
                    "material_request_id": material_request.material_request_id,
                    "display_material_request_id": display_id,
                    "requester_name": material_request.requester_name,
                    "project": material_request.project,
                    "date": material_request.date,
                    "status": material_request.status,
                    "drone_instance_id": drone_instance.id,
                    "drone_instance_code": drone_instance.instance_code,
                    "drone_instance_suffix": drone_instance.suffix,
                    "drone_instance_status": drone_instance.status,
                    "drone_quantity": 1,
                    "total_available_quantity": sum(
                        int(item.get("issued_quantity") or 0) for item in components
                    ),
                    "components": components,
                })

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

        if request_type in {"R&D", "RD"}:
            manager = material_request.rd_items
        elif request_type in {
            "RETURNABLE",
            "RETAIL_SALES",
        }:
            manager = material_request.request_items
        else:
            manager = material_request.bom_items

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
        Create a full rebuild MR from the original MR structure.

        Reusable serials stay attached directly to the NEW rebuilt MR and
        are tagged on the cloned MR rows. Every original BOM/Custom BOM/R&D
        component is cloned. Only the missing quantity enters Central In Store
        / Procurement routing.
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

        # From-Scrap MR uses the same human-readable requester name
        # shown in Manager / Finance Scrap notifications.
        requester_name = (
            cls.get_scrap_requester_display_name(
                scrap_entry,
                requester,
            )
        )

        workflow_metadata = (
            scrap_entry.inventory_allocations
            if isinstance(
                scrap_entry.inventory_allocations,
                dict,
            )
            else {}
        )

        explicit_good_items = (
            workflow_metadata.get("good_items")
            or workflow_metadata.get("selected_items")
            or []
        )

        # Physical-drone Scrap: rebuild exactly the selected _01/_02 unit,
        # never the aggregate component quantities of the original multi-drone MR.
        source_drone_instance_id = workflow_metadata.get("drone_instance_id")
        instance_component_quantities = {}
        if source_drone_instance_id:
            source_instance = (
                DroneInstance.objects.filter(
                    pk=source_drone_instance_id,
                    material_request=source_mr,
                ).first()
            )
            if source_instance is not None:
                instance_component_quantities = {
                    int(row.component_id): int(row.quantity or 0)
                    for row in DroneComponentAllocation.objects.filter(
                        drone_instance=source_instance
                    )
                }

        # Explicit workflow metadata is authoritative. This guarantees the
        # serials tagged as FROM_SCRAP_SERIALS are GOOD / reusable serials,
        # never damaged/reorder serials.
        if explicit_good_items:
            scrap_items = explicit_good_items

        recovered_quantity = sum(
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

        source_request_type = str(
            source_mr.request_type
            or ""
        ).strip().upper()

        if source_request_type in {
            "R&D",
            "RD",
        }:
            source_items_manager = (
                source_mr.rd_items
            )
            item_model = RDItem
        elif source_request_type in {
            "RETURNABLE",
            "RETAIL_SALES",
        }:
            source_items_manager = (
                source_mr.request_items
            )
            item_model = RequestItem
        else:
            source_items_manager = (
                source_mr.bom_items
            )
            item_model = BOMItem

        # Only component-bearing positive-quantity rows are valid rebuild
        # blueprint rows. Old records can contain empty/stale item rows.
        source_items = [
            item
            for item in (
                source_items_manager
                .select_related(
                    "component"
                )
                .all()
                .order_by("id")
            )
            if getattr(
                item,
                "component_id",
                None,
            )
            and max(
                int(
                    getattr(
                        item,
                        "quantity",
                        0,
                    )
                    or 0
                ),
                0,
            ) > 0
        ]

        # ---------------------------------------------------------
        # LEGACY FALLBACK
        #
        # Some already-issued historical MRs have ProjectInventory rows
        # but no BOMItem / RDItem / RequestItem rows. That is exactly the
        # condition that previously produced:
        #
        #   {"items":["Material Request has no valid components."]}
        #
        # ProjectInventory is sufficient to rebuild the component+quantity
        # blueprint for those old MRs.
        # ---------------------------------------------------------
        source_project_rows = []

        if not source_items:
            source_project_rows = list(
                ProjectInventory.objects
                .select_for_update()
                .select_related(
                    "component"
                )
                .filter(
                    material_request=
                        source_mr
                )
                .order_by("id")
            )

            source_project_rows = [
                row
                for row in source_project_rows
                if getattr(
                    row,
                    "component_id",
                    None,
                )
                and max(
                    int(
                        getattr(
                            row,
                            "requested_quantity",
                            0,
                        )
                        or 0
                    ),
                    0,
                ) > 0
            ]

        if (
            not source_items
            and not source_project_rows
        ):
            raise ValidationError(
                {
                    "items": [
                        (
                            "Source Material Request "
                            f"{source_mr.material_request_id} has no "
                            "component rows or Project Inventory blueprint."
                        )
                    ]
                }
            )

        total_quantity = (
            sum(
                max(
                    int(
                        getattr(
                            item,
                            "quantity",
                            0,
                        )
                        or 0
                    ),
                    0,
                )
                for item in source_items
            )
            if source_items
            else sum(
                max(
                    int(
                        getattr(
                            row,
                            "requested_quantity",
                            0,
                        )
                        or 0
                    ),
                    0,
                )
                for row in source_project_rows
            )
        )

        # ---------------------------------------------------------
        # FROM-SCRAP REORDER TYPE
        # ---------------------------------------------------------
        # GOOD / recovered quantity = 0
        #     -> every issued component/serial was Scrap
        #     -> FULLY REORDERED
        #     -> MR-xxxx_FR
        #
        # GOOD / recovered quantity > 0
        #     -> some components are reused and only damaged/missing
        #        quantities are rebuilt
        #     -> PARTIALLY REORDERED
        #     -> MR-xxxx_PR
        # ---------------------------------------------------------
        reorder_suffix = (
            "PR"
            if recovered_quantity > 0
            else "FR"
        )

        reorder_mr_number = (
            f"{source_mr.material_request_id}_{reorder_suffix}"
        )

        reorder_mr = MaterialRequest.objects.create(
            material_request_id=
                reorder_mr_number,

            requester_name=requester_name,
            requester=requester,
            date=timezone.localdate(),
            project=source_mr.project,

            # Preserve the original MR definition exactly.
            bom=source_mr.bom,
            customized_bom=source_mr.customized_bom,
            request_type=source_mr.request_type,

            required_quantity=(
                1
                if source_drone_instance_id
                else max(
                    int(getattr(source_mr, "required_quantity", 1) or 1),
                    1,
                )
            ),
            required_date=(
                source_mr.required_date
            ),
            remarks=(
                f"Automatically created From Scrap for "
                f"{source_mr.material_request_id}"
            ),
            status="PENDING_MANAGER",
            approval_status="PENDING_MANAGER",
            po_raised=False,
        )

        recovered_by_component = {}

        for item in scrap_items:
            component_key = str(
                item.get("component")
                or ""
            ).strip()

            if not component_key:
                continue

            recovered_by_component[
                component_key
            ] = cls.normalize_serials(
                recovered_by_component.get(
                    component_key,
                    [],
                )
                + cls.normalize_serials(
                    item.get(
                        "serial_numbers"
                    )
                    or []
                )
            )

        # Reorder YES:
        # GOOD / recovered components stay directly attached to the NEW MR.
        # They are NOT returned to Central In Store.
        #
        # The cloned MR rows contain FROM_SCRAP_SERIALS. The Material Request
        # router treats those serials as already fulfilled and sends ONLY the
        # missing quantity to In Store / Procurement.

        if source_items:
            for source_item in source_items:
                physical_component_quantity = (
                    instance_component_quantities.get(
                        int(getattr(source_item, "component_id", 0) or 0),
                        0,
                    )
                    if instance_component_quantities
                    else None
                )
                if instance_component_quantities and physical_component_quantity <= 0:
                    continue

                serial_numbers = (
                    recovered_by_component.get(
                        str(
                            getattr(
                                source_item,
                                "component_id",
                                "",
                            )
                        ),
                        [],
                    )
                )

                marker = (
                    f"FROM_SCRAP_SERIALS:{'|'.join(serial_numbers)}"
                    f"\nSOURCE_SCRAP:{scrap_entry.code}"
                    f"\nSOURCE_MR:{source_mr.material_request_id}"
                    f"\nCOMPONENT_SOURCE:{'RECOVERED_FROM_SCRAP' if serial_numbers else 'IN_STORE_OR_PROCUREMENT'}"
                )

                clone = item_model()

                # Clone every compatible concrete field from the exact
                # original MR row.
                for field in (
                    source_item
                    ._meta
                    .concrete_fields
                ):
                    if (
                        field.primary_key
                        or field.name
                        == "material_request"
                    ):
                        continue

                    if any(
                        candidate.name
                        == field.name
                        for candidate
                        in clone
                        ._meta
                        .concrete_fields
                    ):
                        setattr(
                            clone,
                            field.attname,
                            getattr(
                                source_item,
                                field.attname,
                            ),
                        )

                clone.material_request = (
                    reorder_mr
                )

                if physical_component_quantity is not None and hasattr(clone, "quantity"):
                    clone.quantity = physical_component_quantity

                for quantity_field in (
                    "po_raised_quantity",
                    "delivered_quantity",
                    "qc_passed_quantity",
                    "qc_failed_quantity",
                    "project_inventory_quantity",
                ):
                    if hasattr(
                        clone,
                        quantity_field,
                    ):
                        setattr(
                            clone,
                            quantity_field,
                            0,
                        )

                if hasattr(
                    clone,
                    "inventory_quantity",
                ):
                    # Recovered Scrap is not Central In Store stock.
                    clone.inventory_quantity = 0

                if hasattr(
                    clone,
                    "vendor",
                ):
                    clone.vendor = None

                if hasattr(
                    clone,
                    "remarks",
                ):
                    existing_remarks = str(
                        getattr(
                            source_item,
                            "remarks",
                            "",
                        )
                        or ""
                    ).strip()

                    clone.remarks = (
                        f"{existing_remarks}\n{marker}"
                        .strip()
                    )

                clone.save()

        else:
            # Legacy issued MR fallback. Reconstruct a minimal valid item row
            # from ProjectInventory so Finance approval can route the missing
            # quantity instead of failing with "no valid components".
            for project_row in source_project_rows:
                component = (
                    project_row.component
                )

                component_id = (
                    project_row.component_id
                )

                required_quantity = (
                    max(
                        int(instance_component_quantities.get(int(component_id), 0)),
                        0,
                    )
                    if instance_component_quantities
                    else max(int(project_row.requested_quantity or 0), 0)
                )

                if instance_component_quantities and required_quantity <= 0:
                    continue

                serial_numbers = (
                    recovered_by_component.get(
                        str(
                            component_id
                        ),
                        [],
                    )
                )

                marker = (
                    f"FROM_SCRAP_SERIALS:{'|'.join(serial_numbers)}"
                    f"\nSOURCE_SCRAP:{scrap_entry.code}"
                    f"\nSOURCE_MR:{source_mr.material_request_id}"
                    "\nLEGACY_BLUEPRINT:PROJECT_INVENTORY"
                    f"\nCOMPONENT_SOURCE:{'RECOVERED_FROM_SCRAP' if serial_numbers else 'IN_STORE_OR_PROCUREMENT'}"
                )

                common_kwargs = {
                    "material_request":
                        reorder_mr,
                    "component":
                        component,
                    "category":
                        (
                            getattr(
                                component,
                                "category",
                                "",
                            )
                            or ""
                        ),
                    "quantity":
                        required_quantity,
                    "unit":
                        "pc",
                    "inventory_quantity":
                        0,
                    "po_raised_quantity":
                        0,
                    "delivered_quantity":
                        0,
                    "qc_passed_quantity":
                        0,
                    "qc_failed_quantity":
                        0,
                    "project_inventory_quantity":
                        0,
                    "vendor":
                        None,
                    "remarks":
                        marker,
                }

                if item_model is BOMItem:
                    BOMItem.objects.create(
                        **common_kwargs,
                        specification=(
                            getattr(
                                component,
                                "specifications",
                                "",
                            )
                            or ""
                        ),
                        unit_price=0,
                        price=0,
                        tax=0,
                    )
                elif item_model is RDItem:
                    RDItem.objects.create(
                        **common_kwargs,
                        specifications=(
                            getattr(
                                component,
                                "specifications",
                                "",
                            )
                            or ""
                        ),
                        unit_price=0,
                        price=0,
                        tax=0,
                        total_price=0,
                    )
                else:
                    RequestItem.objects.create(
                        **common_kwargs,
                        specifications=(
                            getattr(
                                component,
                                "specifications",
                                "",
                            )
                            or ""
                        ),
                    )

        # Finance has already approved the parent Scrap. Do not request a
        # second Manager approval for the generated MR. Route it immediately
        # through the normal live-stock split:
        #   available stock -> Inventory
        #   shortage        -> Procurement
        from materialrequest.views import MaterialRequestViewSet

        # Defensive verification: Finance approval must never route an empty
        # generated MR.
        generated_items = (
            reorder_mr.rd_items.all()
            if source_request_type in {"R&D", "RD"}
            else (
                reorder_mr.request_items.all()
                if source_request_type in {
                    "RETURNABLE",
                    "RETAIL_SALES",
                }
                else reorder_mr.bom_items.all()
            )
        )

        if not generated_items.filter(
            component__isnull=False,
            quantity__gt=0,
        ).exists():
            raise ValidationError(
                {
                    "items": [
                        (
                            "From-Scrap MR component cloning failed for "
                            f"{source_mr.material_request_id}. "
                            "No valid component rows were generated."
                        )
                    ]
                }
            )

        MaterialRequestViewSet().route_after_manager_approval(
            reorder_mr,
            approval_source="FINANCE",
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
    def create_returnable_qc_reorder_mr(
        cls,
        *,
        scrap_entry,
        source_mr,
        failed_items,
        good_items,
    ):
        """Create the visible PR/FR child MR for failed returned-drone parts.

        <ORIGINAL>_PR -> at least one returned component passed QC.
        <ORIGINAL>_FR -> every returned component failed QC.

        The child MR is a tracking/audit row under the original MR. Only the
        failed quantities are copied. Procurement still raises the replacement
        PO from the Returnable QC Failed notification, which guarantees that a
        Manager YES always reaches Procurement even when Central In Store has
        stock available.
        """
        if source_mr is None:
            raise ValidationError({
                "detail": "Source Material Request for returned drone was not found."
            })

        failed_items = [
            item
            for item in (failed_items or [])
            if isinstance(item, dict)
            and item.get("component")
            and max(int(item.get("quantity") or 0), 0) > 0
        ]
        if not failed_items:
            raise ValidationError({
                "items": ["No failed returned-drone components are available for reorder."]
            })

        good_quantity = sum(
            max(int(item.get("quantity") or 0), 0)
            for item in (good_items or [])
            if isinstance(item, dict)
        )
        suffix = "PR" if good_quantity > 0 else "FR"
        child_number = f"{source_mr.material_request_id}_{suffix}"

        existing = MaterialRequest.objects.filter(
            material_request_id=child_number
        ).first()
        if existing is not None:
            if "RETURNABLE_QC_REORDER" in str(existing.remarks or "").upper():
                return existing
            raise ValidationError({
                "material_request_id": f"{child_number} already exists for another workflow."
            })

        marker_text = (
            "RETURNABLE_QC_REORDER\n"
            f"SOURCE_MR:{source_mr.material_request_id}\n"
            f"REORDER_TYPE:{suffix}\n"
            f"RETURNABLE_QC_OUTWARD:{scrap_entry.pk}"
        )

        child = MaterialRequest.objects.create(
            material_request_id=child_number,
            requester_name=(
                source_mr.requester_name
                or scrap_entry.requested_by
                or "Engineer"
            ),
            requester=source_mr.requester,
            date=timezone.localdate(),
            project=source_mr.project,
            bom=source_mr.bom,
            customized_bom=source_mr.customized_bom,
            request_type=source_mr.request_type,
            required_quantity=max(
                int(getattr(source_mr, "required_quantity", 1) or 1),
                1,
            ),
            required_date=source_mr.required_date,
            remarks=(
                f"Returnable QC {'Partial' if suffix == 'PR' else 'Full'} Reorder.\n"
                f"{marker_text}"
            ),
            status="PROCUREMENT_PENDING",
            approval_status="MANAGER_APPROVED",
            po_raised=False,
        )

        request_type = str(source_mr.request_type or "").strip().upper()
        if request_type in {"R&D", "RD"}:
            item_model = RDItem
        elif request_type in {"RETURNABLE", "RETAIL_SALES"}:
            item_model = RequestItem
        else:
            item_model = BOMItem

        for failed in failed_items:
            component_id = failed.get("component")
            quantity = max(int(failed.get("quantity") or 0), 0)
            if not component_id or quantity <= 0:
                continue

            source_item = cls.get_source_mr_item(source_mr, component_id)
            item_marker = (
                f"RETURNABLE_QC_FAILED_QTY:{quantity}\n"
                f"SOURCE_MR:{source_mr.material_request_id}\n"
                f"RETURNABLE_QC_OUTWARD:{scrap_entry.pk}"
            )

            if item_model is BOMItem:
                unit_price = getattr(source_item, "unit_price", 0) or 0
                BOMItem.objects.create(
                    material_request=child,
                    component_id=component_id,
                    category=getattr(source_item, "category", "") or "",
                    specification=getattr(source_item, "specification", "") or "",
                    quantity=quantity,
                    unit=getattr(source_item, "unit", "pc") or "pc",
                    unit_price=unit_price,
                    price=unit_price * quantity,
                    tax=getattr(source_item, "tax", 0) or 0,
                    inventory_quantity=0,
                    po_raised_quantity=0,
                    delivered_quantity=0,
                    qc_passed_quantity=0,
                    qc_failed_quantity=0,
                    project_inventory_quantity=0,
                    vendor=getattr(source_item, "vendor", None),
                    remarks=item_marker,
                )
            elif item_model is RDItem:
                unit_price = getattr(source_item, "unit_price", 0) or 0
                RDItem.objects.create(
                    material_request=child,
                    component_id=component_id,
                    category=getattr(source_item, "category", "") or "",
                    specifications=getattr(source_item, "specifications", "") or "",
                    quantity=quantity,
                    unit_price=unit_price,
                    unit=getattr(source_item, "unit", "pc") or "pc",
                    price=unit_price * quantity,
                    tax=getattr(source_item, "tax", 0) or 0,
                    total_price=0,
                    inventory_quantity=0,
                    po_raised_quantity=0,
                    delivered_quantity=0,
                    qc_passed_quantity=0,
                    qc_failed_quantity=0,
                    project_inventory_quantity=0,
                    vendor=getattr(source_item, "vendor", None),
                    remarks=item_marker,
                )
            else:
                RequestItem.objects.create(
                    material_request=child,
                    component_id=component_id,
                    category=getattr(source_item, "category", "") or "",
                    specifications=getattr(source_item, "specifications", "") or "",
                    quantity=quantity,
                    unit=getattr(source_item, "unit", "pc") or "pc",
                    inventory_quantity=0,
                    po_raised_quantity=0,
                    delivered_quantity=0,
                    qc_passed_quantity=0,
                    qc_failed_quantity=0,
                    project_inventory_quantity=0,
                    vendor=getattr(source_item, "vendor", None),
                    remarks=item_marker,
                )

        return child

    @classmethod
    def process_engineer_scrap_disposition(
        cls,
        *,
        scrap_entry,
    ):
        """
        Execute the final Finance-approved disposition.

        Supported workflows:
        - ENGINEER_MR_SCRAP_DISPOSITION_V1 (existing Engineer Scrap)
        - RETURNABLE_DRONE_QC_V1          (returned assembled drone QC fail)
        - RETURNABLE_COMPONENT_QC_V1      (returned loose component QC fail)

        Nothing is restored/rebuilt/procured before Finance approval.
        """
        metadata = (
            scrap_entry.inventory_allocations
            if isinstance(scrap_entry.inventory_allocations, dict)
            else {}
        )
        workflow = str(metadata.get("workflow") or "").strip().upper()
        supported = {
            "ENGINEER_MR_SCRAP_DISPOSITION_V1",
            "RETURNABLE_DRONE_QC_V1",
            "RETURNABLE_COMPONENT_QC_V1",
        }
        if workflow not in supported:
            return metadata
        if metadata.get("disposition_processed"):
            return metadata

        source_mr = None
        if scrap_entry.material_request_id:
            source_mr = (
                MaterialRequest.objects.select_for_update()
                .filter(pk=scrap_entry.material_request_id)
                .first()
            )

        reorder_choice = str(metadata.get("reorder_choice", "NONE") or "NONE").strip().upper()
        selected_items = metadata.get("selected_items", []) or []
        return_items = metadata.get("return_items", []) or []
        reorder_items = metadata.get("reorder_items", []) or []
        returned_inventory_ids = []
        reorder_mr = None

        if workflow == "RETURNABLE_COMPONENT_QC_V1":
            # Failed returned components remain Scrap after Finance approval.
            # Restore=YES means Procurement is now allowed to raise a replacement PO.
            metadata["procurement_restore_ready"] = reorder_choice == "YES"
            metadata["procurement_restore_status"] = (
                "PENDING_PROCUREMENT" if reorder_choice == "YES" else "NOT_REQUIRED"
            )

        elif workflow == "RETURNABLE_DRONE_QC_V1":
            if source_mr is None:
                raise ValidationError({"detail": "Source Material Request for returned drone was not found."})

            if reorder_choice == "NO":
                # GOOD units were already returned to Central In Store at
                # Inventory Return QC. Finance NO only finalizes BAD units as
                # Scrap, so never create a second Inventory row here.
                returned_inventory_ids = list(
                    metadata.get("returned_inventory_ids") or []
                )

            elif reorder_choice == "YES":
                # New Returnable workflow creates the child PR/FR MR at
                # Manager YES. Keep Finance-stage processing idempotent for
                # legacy records that may still reach this method.
                reorder_mr_id = metadata.get("replacement_mr_id")
                reorder_mr = (
                    MaterialRequest.objects.filter(pk=reorder_mr_id).first()
                    if reorder_mr_id
                    else None
                )

        else:
            # Engineer MR Scrap disposition:
            #
            # PARTIAL + NO:
            #   GOOD / unselected serials -> In Store
            #
            # TOTAL + NO:
            #   all selected serials remain final Scrap
            #
            # PARTIAL + YES:
            #   GOOD serials are reused -> new <ORIGINAL>_PR
            #
            # TOTAL + YES:
            #   there are no GOOD serials -> full rebuild -> <ORIGINAL>_FR
            scrap_mode = str(
                metadata.get(
                    "scrap_mode",
                    "PARTIAL",
                )
                or "PARTIAL"
            ).strip().upper()

            if reorder_choice == "NO":
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

            elif reorder_choice == "YES":
                # From-Scrap routing rule:
                #
                # GOOD / reusable serials are fulfilled immediately by the
                # generated MR. DAMAGED / Scrap serials are the missing
                # quantity and must go through Central In Store / Procurement.
                #
                # IMPORTANT:
                # reorder_items contains the DAMAGED/Scrap side after Manager
                # chooses YES. It must NEVER be passed as recovered stock.
                #
                # good_items is explicit in the current workflow; selected_items
                # remains the backward-compatible GOOD/reusable alias.
                reusable_items = (
                    metadata.get("good_items")
                    or selected_items
                    or []
                )

                reorder_mr = (
                    cls.create_scrap_reorder_mr(
                        scrap_entry=scrap_entry,
                        source_mr=source_mr,
                        scrap_items=reusable_items,
                    )
                )

        metadata["disposition_processed"] = True
        metadata["processed_at"] = timezone.now().isoformat()
        metadata["returned_inventory_ids"] = returned_inventory_ids
        metadata["replacement_mr_id"] = (
            reorder_mr.id
            if reorder_mr
            else None
        )
        metadata["replacement_mr_number"] = (
            reorder_mr.material_request_id
            if reorder_mr
            else ""
        )

        if reorder_mr:
            replacement_number = str(
                reorder_mr.material_request_id
                or ""
            ).upper()

            if replacement_number.endswith(
                "_FR"
            ):
                metadata[
                    "reorder_type"
                ] = "FULL_REORDER"
            elif replacement_number.endswith(
                "_PR"
            ):
                metadata[
                    "reorder_type"
                ] = "PARTIAL_REORDER"

        return metadata

    @staticmethod
    def sync_returnable_usage_status(metadata, approval_status, *, reason=""):
        """Synchronize ComponentUsage rows linked to a Returnable QC Scrap."""
        if not isinstance(metadata, dict):
            return
        usage_ids = [
            value for value in (metadata.get("returnable_usage_ids") or [])
            if str(value).strip()
        ]
        if not usage_ids:
            return
        from componentusage.models import ComponentUsage
        rows = ComponentUsage.objects.select_for_update().filter(pk__in=usage_ids)
        for usage in rows:
            usage.return_approval_status = approval_status
            if reason:
                current = str(usage.return_reason or "").strip()
                usage.return_reason = f"{current}\n{reason}".strip()

            details = (
                usage.inventory_issue_details
                if isinstance(usage.inventory_issue_details, list)
                else []
            )
            if not details:
                details = [{}]
            elif not isinstance(details[0], dict):
                details[0] = {}

            details[0]["scrap_approval_status"] = approval_status
            if reason:
                details[0]["scrap_approval_reason"] = reason

            if metadata.get("procurement_restore_status"):
                details[0]["restore_status"] = metadata.get(
                    "procurement_restore_status"
                )
            if metadata.get("procurement_restore_ready") is not None:
                details[0]["procurement_restore_ready"] = bool(
                    metadata.get("procurement_restore_ready")
                )
            if metadata.get("replacement_mr_number"):
                details[0]["replacement_mr_number"] = metadata.get(
                    "replacement_mr_number"
                )
            if metadata.get("restore_po_number"):
                details[0]["restore_po_number"] = metadata.get(
                    "restore_po_number"
                )

            usage.inventory_issue_details = details
            usage.save(
                update_fields=[
                    "return_approval_status",
                    "return_reason",
                    "inventory_issue_details",
                ]
            )

    @staticmethod
    def generate_restore_po_number():
        """Generate the same NN/YY-YY PO format used by the frontend."""
        today = timezone.localdate()
        start_year = today.year if today.month >= 4 else today.year - 1
        fy = f"{str(start_year)[-2:]}-{str(start_year + 1)[-2:]}"
        highest = 0
        for value in PurchaseOrder.objects.filter(po_number__endswith=f"/{fy}").values_list("po_number", flat=True):
            try:
                prefix = str(value).split("/", 1)[0]
                highest = max(highest, int(prefix))
            except (TypeError, ValueError):
                continue
        return f"{highest + 1:02d}/{fy}"

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

    # =========================================================
    # IN-DRONE SALES: FINANCE -> MANAGEMENT APPROVAL
    # =========================================================
    # This flow is deliberately separate from normal direct SALES.
    # Components are already physically issued to the MR/In-Drone, so
    # creating a Sales approval request MUST NOT deduct In-Store stock again.

    @classmethod
    def get_active_role(cls, request):
        """
        Return the role selected for the current authenticated session.

        JWT `active_role` is authoritative for request authorization.
        Falling back to the primary DB role keeps older tokens compatible.
        """
        token = getattr(request, "auth", None)
        token_role = ""

        if token is not None:
            try:
                token_role = cls.normalize_role_value(
                    token.get("active_role", "")
                )
            except (
                AttributeError,
                TypeError,
                ValueError,
            ):
                token_role = ""

        if token_role:
            return token_role

        return cls.normalize_role_value(
            getattr(
                getattr(
                    request,
                    "user",
                    None,
                ),
                "role",
                "",
            )
        )

    @classmethod
    def require_finance_for_in_drone_sales(cls, request):
        user = getattr(request, "user", None)

        if not user or not getattr(user, "is_authenticated", False):
            raise PermissionDenied("Authentication is required.")

        role = cls.get_active_role(request)

        if role not in {"finance", "admin"} and not getattr(
            user, "is_superuser", False
        ):
            raise PermissionDenied(
                "Only Finance can send an In-Drone request for Sales approval."
            )

        return user

    @classmethod
    def require_management_for_sales(cls, request):
        user = getattr(request, "user", None)

        if not user or not getattr(user, "is_authenticated", False):
            raise PermissionDenied("Authentication is required.")

        role = cls.get_active_role(request)

        if role not in {"management", "admin"} and not getattr(
            user, "is_superuser", False
        ):
            raise PermissionDenied(
                "Only Management can approve or reject Sales."
            )

        return user

    @classmethod
    def get_assigned_roles(cls, user):
        if not user:
            return set()

        get_all_roles = getattr(user, "get_all_roles", None)

        if callable(get_all_roles):
            try:
                values = get_all_roles() or []
            except Exception:
                values = []
        else:
            values = [
                getattr(user, "role", ""),
                *(getattr(user, "additional_roles", []) or []),
            ]

        return {
            cls.normalize_role_value(value)
            for value in values
            if cls.normalize_role_value(value)
        }

    @classmethod
    def get_management_email_users(cls):
        candidates = (
            User.objects
            .filter(is_active=True)
            .exclude(email__isnull=True)
            .exclude(email="")
            .order_by("id")
        )

        result = []
        seen = set()

        for candidate in candidates:
            if "management" not in cls.get_assigned_roles(candidate):
                continue

            email_key = str(candidate.email or "").strip().casefold()
            if not email_key or email_key in seen:
                continue

            seen.add(email_key)
            result.append(candidate)

        return result

    @classmethod
    def send_management_sales_approval_email(cls, outward_id):
        try:
            first_row = (
                OutwardEntry.objects
                .select_related("material_request")
                .get(pk=outward_id, outward_type="SALES")
            )
        except OutwardEntry.DoesNotExist:
            return False

        if str(first_row.approval_status or "").strip().upper() != "PENDING_MANAGEMENT":
            return False

        material_request = first_row.material_request
        mr_number = (
            getattr(material_request, "material_request_id", "")
            or "-"
        )

        sales_rows = OutwardEntry.objects.filter(
            outward_type="SALES",
            material_request=material_request,
        ).select_related("component")

        component_summary = ", ".join(
            f"{(getattr(row.component, 'name', '') or row.product_name or 'Component')}-{int(row.quantity or 0)}"
            for row in sales_rows
        ) or "-"

        requester_name = str(first_row.requested_by or "Finance").strip() or "Finance"
        subject = f"{mr_number} - Sales Approval Required"
        sent_any = False

        for management_user in cls.get_management_email_users():
            try:
                sent = send_ipms_email(
                    recipient_email=management_user.email,
                    subject=subject,
                    context={
                        "recipient_name": cls.get_mail_user_name(
                            management_user,
                            "Management",
                        ),
                        "message": (
                            f"Finance submitted In-Drone Sales for {mr_number}. "
                            "Management approval is required before the Sales is finalized."
                        ),
                        "table_headers": [
                            "Material Request",
                            "Components",
                            "Requested By",
                            "Status",
                        ],
                        "table_values": [
                            mr_number,
                            component_summary,
                            requester_name,
                            "Pending Management",
                        ],
                        "status": "Pending Management",
                        "instruction": (
                            "Please review the Sales request and approve or reject it in IPMS."
                        ),
                        "button_text": "Review Sales in IPMS",
                        "action_url": (
                            f"{cls.get_ipms_base_url()}"
                            f"/management-notifications"
                        ),
                    },
                )
                if sent:
                    sent_any = True
            except Exception as exc:
                print(
                    "SALES MANAGEMENT EMAIL ERROR:",
                    management_user.email,
                    exc,
                )

        return sent_any

    @staticmethod
    def _resolve_material_request(reference):
        raw = str(reference or "").strip()
        if not raw:
            raise ValidationError(
                {"material_request_id": "Material Request is required."}
            )

        query = Q(material_request_id=raw)
        if raw.isdigit():
            query |= Q(pk=int(raw))

        material_request = (
            MaterialRequest.objects
            .filter(query)
            .first()
        )

        if material_request is None:
            raise ValidationError(
                {"material_request_id": "Material Request was not found."}
            )

        return material_request

    @staticmethod
    def _project_row_issued_quantity(project_row):
        direct = getattr(project_row, "calculated_issued_quantity", None)
        if direct is not None:
            try:
                return max(int(direct or 0), 0)
            except (TypeError, ValueError):
                pass

        return max(
            int(getattr(project_row, "issued_store_quantity", 0) or 0),
            0,
        ) + max(
            int(getattr(project_row, "issued_purchased_quantity", 0) or 0),
            0,
        )

    @staticmethod
    def _drone_instance_from_reference(material_request, reference, *, lock=False):
        raw = str(reference or "").strip()
        if not raw:
            return None

        queryset = DroneInstance.objects.filter(material_request=material_request)
        if lock:
            queryset = queryset.select_for_update()

        query = Q(instance_code__iexact=raw)
        if raw.isdigit():
            query |= Q(pk=int(raw))

        instance = queryset.filter(query).first()
        if instance is not None:
            return instance

        # Friendly suffix input such as _01 / 01.
        suffix = raw.lstrip("_")
        if suffix.isdigit():
            return queryset.filter(sequence=int(suffix)).first()
        return None

    @staticmethod
    def _sales_metadata(row):
        value = getattr(row, "inventory_allocations", None)
        return value if isinstance(value, dict) else {}

    @classmethod
    def _drone_instance_from_sales_row(cls, row, *, lock=False):
        metadata = cls._sales_metadata(row)
        instance_id = metadata.get("drone_instance_id")
        instance_code = metadata.get("drone_instance_code")
        material_request = getattr(row, "material_request", None)
        if material_request is None:
            return None
        return cls._drone_instance_from_reference(
            material_request,
            instance_id or instance_code,
            lock=lock,
        )

    @classmethod
    def _sales_rows_for_instance(cls, instance, *, lock=False):
        """Return only the component Sales rows in the same physical-drone batch."""
        queryset = OutwardEntry.objects.filter(outward_type="SALES")
        if instance.material_request_id:
            queryset = queryset.filter(material_request_id=instance.material_request_id)
        else:
            queryset = queryset.filter(pk=instance.pk)
        if lock:
            queryset = queryset.select_for_update()

        candidates = list(queryset.order_by("id"))
        metadata = cls._sales_metadata(instance)
        batch_id = str(metadata.get("sales_batch_id") or "").strip()
        drone_instance_id = str(metadata.get("drone_instance_id") or "").strip()

        if batch_id:
            matched = [
                row for row in candidates
                if str(cls._sales_metadata(row).get("sales_batch_id") or "").strip() == batch_id
            ]
            if matched:
                return matched

        if drone_instance_id:
            matched = [
                row for row in candidates
                if str(cls._sales_metadata(row).get("drone_instance_id") or "").strip()
                == drone_instance_id
            ]
            if matched:
                return matched

        # Legacy rows created before physical DroneInstance tracking.
        return candidates

    @action(
        detail=False,
        methods=["post"],
        url_path="in-drone-sales",
    )
    def in_drone_sales(self, request):
        """
        Send ONE physical In-Drone instance to Sales.

        Each MR can contain many physical drones (_01, _02, ...). Sales now
        copies only the exact component serials assigned to the selected
        DroneInstance. A sibling drone under the same MR stays AVAILABLE.
        """
        user = self.require_finance_for_in_drone_sales(request)
        material_request = self._resolve_material_request(
            request.data.get("material_request_id")
            or request.data.get("materialRequestId")
        )

        current_mr_status = str(material_request.status or "").strip().upper()
        if current_mr_status not in {
            "INVENTORY_ISSUED", "MR_COMPLETED", "ISSUED", "COMPLETED"
        }:
            raise ValidationError({
                "detail": (
                    "Sales can be requested only after every component has been "
                    "issued and the Material Request is in In Drone."
                )
            })

        ensure_drone_instances(material_request)
        refresh_drone_instance_statuses(material_request)

        raw_instance = (
            request.data.get("drone_instance_id")
            or request.data.get("droneInstanceId")
            or request.data.get("drone_instance_code")
            or request.data.get("droneInstanceCode")
        )

        with transaction.atomic():
            drone_instance = self._drone_instance_from_reference(
                material_request,
                raw_instance,
                lock=True,
            )

            # Backward-compatible one-unit request: choose the first free
            # physical drone when an older frontend does not send an instance.
            if drone_instance is None and not str(raw_instance or "").strip():
                drone_instance = (
                    DroneInstance.objects.select_for_update()
                    .filter(material_request=material_request, status="AVAILABLE")
                    .order_by("sequence")
                    .first()
                )

            if drone_instance is None:
                raise ValidationError({
                    "drone_instance_id": "Select a valid physical In-Drone instance."
                })

            if str(drone_instance.status or "").upper() != "AVAILABLE":
                raise ValidationError({
                    "detail": (
                        f"{drone_instance.instance_code} is not available for Sales. "
                        f"Current state: {drone_instance.get_status_display()}."
                    )
                })

            requested_quantity = int(request.data.get("quantity") or 1)
            if requested_quantity != 1:
                raise ValidationError({
                    "quantity": (
                        "A physical Drone Instance always represents one drone. "
                        "Submit Sale separately for each _01/_02 instance."
                    )
                })

            allocations = list(
                DroneComponentAllocation.objects.select_related("component")
                .filter(drone_instance=drone_instance, quantity__gt=0)
                .order_by("component_id")
            )
            if not allocations:
                raise ValidationError({
                    "detail": "This physical drone has no component allocation details."
                })

            requester_name = self.get_actor_name(user)
            client = str(request.data.get("client") or "").strip()
            invoice_number = str(
                request.data.get("invoice_number")
                or request.data.get("invoiceNumber")
                or ""
            ).strip()
            remarks = str(request.data.get("remarks") or "").strip()

            existing_rows = list(
                OutwardEntry.objects.select_for_update()
                .filter(outward_type="SALES", material_request=material_request)
                .order_by("id")
            )
            instance_existing = [
                row for row in existing_rows
                if str(self._sales_metadata(row).get("drone_instance_id") or "")
                == str(drone_instance.pk)
            ]

            if instance_existing:
                existing_statuses = {
                    str(row.approval_status or row.status or "").strip().upper()
                    for row in instance_existing
                }
                if "APPROVED" in existing_statuses:
                    drone_instance.status = "SOLD"
                    drone_instance.save(update_fields=["status", "updated_at"])
                    return Response({
                        "material_request_id": material_request.material_request_id,
                        "drone_instance_id": drone_instance.pk,
                        "drone_instance_code": drone_instance.instance_code,
                        "status": "APPROVED",
                        "detail": "This physical drone is already approved for Sales.",
                        "sales": self.get_serializer(instance_existing, many=True).data,
                    }, status=status.HTTP_200_OK)

                batch_id = str(
                    self._sales_metadata(instance_existing[0]).get("sales_batch_id")
                    or uuid4().hex
                )
                for row in instance_existing:
                    metadata = self._sales_metadata(row)
                    metadata.update({
                        "workflow": "IN_DRONE_PHYSICAL_SALE_V1",
                        "sales_batch_id": batch_id,
                        "drone_instance_id": drone_instance.pk,
                        "drone_instance_code": drone_instance.instance_code,
                    })
                    row.inventory_allocations = metadata
                    row.approval_status = "PENDING_MANAGEMENT"
                    row.status = "PENDING_MANAGEMENT"
                    row.rejection_reason = None
                    row.rejected_by = None
                    row.requested_by = requester_name
                    row.requested_by_user_id = user.pk
                    row.client = client or row.client
                    row.invoice_number = invoice_number or row.invoice_number
                    row.remarks = remarks or row.remarks
                    row.save(update_fields=[
                        "inventory_allocations", "approval_status", "status",
                        "rejection_reason", "rejected_by", "requested_by",
                        "requested_by_user_id", "client", "invoice_number",
                        "remarks", "updated_at",
                    ])
                sales_rows = instance_existing
            else:
                batch_id = uuid4().hex
                sales_rows = []
                for allocation in allocations:
                    component = allocation.component
                    code = str(getattr(component, "component_id", "") or "").strip()
                    name = str(getattr(component, "name", "") or "").strip()
                    label = " - ".join(v for v in (code, name) if v) or code or name or "Component"
                    serials = normalize_drone_serials(allocation.serial_numbers)
                    stamp = timezone.now().strftime("%Y%m%d%H%M%S%f")
                    sales_rows.append(OutwardEntry.objects.create(
                        code=f"OUT-{stamp}-{uuid4().hex[:6].upper()}",
                        outward_type="SALES",
                        item_type="COMPONENT",
                        out_date=timezone.localdate(),
                        product_name=label,
                        component=component,
                        quantity=max(int(allocation.quantity or 0), 1),
                        no_of_components=max(int(allocation.quantity or 0), 1),
                        serial_numbers=serials,
                        inventory_allocations={
                            "workflow": "IN_DRONE_PHYSICAL_SALE_V1",
                            "sales_batch_id": batch_id,
                            "drone_instance_id": drone_instance.pk,
                            "drone_instance_code": drone_instance.instance_code,
                            "drone_instance_suffix": drone_instance.suffix,
                        },
                        stock_deducted=False,
                        stock_restored=False,
                        material_request=material_request,
                        source="DIRECT",
                        requested_by=requester_name,
                        requested_by_user_id=user.pk,
                        client=client or None,
                        invoice_number=invoice_number or None,
                        remarks=remarks or None,
                        approval_status="PENDING_MANAGEMENT",
                        status="PENDING_MANAGEMENT",
                    ))

            drone_instance.status = "SALE_PENDING"
            drone_instance.workflow_metadata = {
                "workflow": "SALES",
                "sales_batch_id": batch_id,
                "sales_row_ids": [row.pk for row in sales_rows],
                "client": client,
                "invoice_number": invoice_number,
            }
            drone_instance.save(update_fields=["status", "workflow_metadata", "updated_at"])

            first_row = sales_rows[0]
            reference_id = str(first_row.pk)
            notification_qs = Notification.objects.filter(
                category="SALES", receiver="MANAGEMENT", reference_id=reference_id
            ).order_by("-id")
            notification = notification_qs.first()
            display_reference = (
                f"{material_request.material_request_id} / {drone_instance.suffix}"
            )
            title = f"Sales Approval Required - {display_reference}"
            message = (
                f"Finance submitted {display_reference} from In Drone for Sales. "
                "Management approval is required."
            )
            if notification is None:
                notification = Notification.objects.create(
                    category="SALES", receiver="MANAGEMENT",
                    reference_id=reference_id, requested_by=requester_name,
                    title=title, message=message,
                    status="PENDING_MANAGEMENT", is_read=False,
                )
            else:
                notification.requested_by = requester_name
                notification.title = title
                notification.message = message
                notification.status = "PENDING_MANAGEMENT"
                notification.is_read = False
                notification.save(update_fields=[
                    "requested_by", "title", "message", "status", "is_read"
                ])
            notification_qs.exclude(pk=notification.pk).delete()
            transaction.on_commit(
                lambda outward_id=first_row.pk: self.send_management_sales_approval_email(outward_id)
            )

        invalidate_outward_cache()
        return Response({
            "material_request_id": material_request.material_request_id,
            "drone_instance_id": drone_instance.pk,
            "drone_instance_code": drone_instance.instance_code,
            "drone_instance_suffix": drone_instance.suffix,
            "status": "PENDING_MANAGEMENT",
            "detail": "Physical drone Sale sent to Management for approval.",
            "sales": self.get_serializer(sales_rows, many=True).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="management-sales-approve")
    def management_sales_approve(self, request, pk=None):
        management_user = self.require_management_for_sales(request)
        with transaction.atomic():
            instance = (
                OutwardEntry.objects.select_for_update()
                .select_related("material_request").get(pk=pk)
            )
            if str(instance.outward_type or "").strip().upper() != "SALES":
                raise ValidationError({"detail": "This action can approve only Sales records."})

            sales_rows = list(self._sales_rows_for_instance(instance, lock=True))
            current_statuses = {
                str(row.approval_status or row.status or "").strip().upper()
                for row in sales_rows
            }
            drone_instance = self._drone_instance_from_sales_row(instance, lock=True)

            if current_statuses != {"APPROVED"}:
                if "PENDING_MANAGEMENT" not in current_statuses:
                    raise ValidationError({
                        "detail": "This Sales request is not pending Management approval."
                    })
                for row in sales_rows:
                    row.approval_status = "APPROVED"
                    row.status = "APPROVED"
                    row.rejection_reason = None
                    row.rejected_by = None
                    row.save(update_fields=[
                        "approval_status", "status", "rejection_reason",
                        "rejected_by", "updated_at",
                    ])

            if drone_instance is not None:
                drone_instance.status = "SOLD"
                metadata = dict(drone_instance.workflow_metadata or {})
                metadata.update({"workflow": "SALES", "approved": True})
                drone_instance.workflow_metadata = metadata
                drone_instance.save(update_fields=["status", "workflow_metadata", "updated_at"])

            first_row = sales_rows[0]
            management_name = self.get_actor_name(management_user)
            Notification.objects.filter(
                category="SALES", receiver="MANAGEMENT", reference_id=str(first_row.pk)
            ).update(
                status="MANAGEMENT_APPROVED", is_read=True,
                message=(
                    "Management approved Sales for "
                    f"{getattr(first_row.material_request, 'material_request_id', '') or first_row.code}."
                ),
            )
            finance_qs = Notification.objects.filter(
                category="SALES", receiver="FINANCE", reference_id=str(first_row.pk)
            ).order_by("-id")
            finance_notification = finance_qs.first()
            finance_message = (
                "Management approved Sales for "
                f"{getattr(first_row.material_request, 'material_request_id', '') or first_row.code}."
            )
            if finance_notification is None:
                finance_notification = Notification.objects.create(
                    category="SALES", receiver="FINANCE", reference_id=str(first_row.pk),
                    requested_by=management_name, title="Sales Approved by Management",
                    message=finance_message, status="APPROVED", is_read=False,
                )
            else:
                finance_notification.requested_by = management_name
                finance_notification.title = "Sales Approved by Management"
                finance_notification.message = finance_message
                finance_notification.status = "APPROVED"
                finance_notification.is_read = False
                finance_notification.save(update_fields=[
                    "requested_by", "title", "message", "status", "is_read"
                ])
            finance_qs.exclude(pk=finance_notification.pk).delete()

        invalidate_outward_cache()
        return Response({
            "status": "APPROVED",
            "detail": "Physical drone Sale approved by Management.",
            "drone_instance_id": getattr(drone_instance, "pk", None),
            "sales": self.get_serializer(sales_rows, many=True).data,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="management-sales-reject")
    def management_sales_reject(self, request, pk=None):
        management_user = self.require_management_for_sales(request)
        reason = str(request.data.get("reason") or request.data.get("rejection_reason") or "").strip()
        if not reason:
            raise ValidationError({"reason": "Rejection reason is required."})

        with transaction.atomic():
            instance = (
                OutwardEntry.objects.select_for_update()
                .select_related("material_request").get(pk=pk)
            )
            if str(instance.outward_type or "").strip().upper() != "SALES":
                raise ValidationError({"detail": "This action can reject only Sales records."})
            sales_rows = list(self._sales_rows_for_instance(instance, lock=True))
            if not any(
                str(row.approval_status or row.status or "").strip().upper()
                == "PENDING_MANAGEMENT"
                for row in sales_rows
            ):
                raise ValidationError({
                    "detail": "This Sales request is not pending Management approval."
                })

            management_name = self.get_actor_name(management_user)
            for row in sales_rows:
                row.approval_status = "MANAGEMENT_REJECTED"
                row.status = "MANAGEMENT_REJECTED"
                row.rejection_reason = reason
                row.rejected_by = management_name
                row.save(update_fields=[
                    "approval_status", "status", "rejection_reason",
                    "rejected_by", "updated_at",
                ])

            drone_instance = self._drone_instance_from_sales_row(instance, lock=True)
            if drone_instance is not None:
                drone_instance.status = "AVAILABLE"
                drone_instance.workflow_metadata = {
                    "workflow": "SALES",
                    "rejected": True,
                    "reason": reason,
                }
                drone_instance.save(update_fields=["status", "workflow_metadata", "updated_at"])

            first_row = sales_rows[0]
            Notification.objects.filter(
                category="SALES", receiver="MANAGEMENT", reference_id=str(first_row.pk)
            ).update(
                status="MANAGEMENT_REJECTED", is_read=True,
                message=(
                    "Management rejected Sales for "
                    f"{getattr(first_row.material_request, 'material_request_id', '') or first_row.code}. "
                    f"Reason: {reason}"
                ),
            )
            finance_qs = Notification.objects.filter(
                category="SALES", receiver="FINANCE", reference_id=str(first_row.pk)
            ).order_by("-id")
            finance_notification = finance_qs.first()
            finance_message = (
                "Management rejected Sales for "
                f"{getattr(first_row.material_request, 'material_request_id', '') or first_row.code}. "
                f"Reason: {reason}"
            )
            if finance_notification is None:
                finance_notification = Notification.objects.create(
                    category="SALES", receiver="FINANCE", reference_id=str(first_row.pk),
                    requested_by=management_name, title="Sales Rejected by Management",
                    message=finance_message, status="MANAGEMENT_REJECTED", is_read=False,
                )
            else:
                finance_notification.requested_by = management_name
                finance_notification.title = "Sales Rejected by Management"
                finance_notification.message = finance_message
                finance_notification.status = "MANAGEMENT_REJECTED"
                finance_notification.is_read = False
                finance_notification.save(update_fields=[
                    "requested_by", "title", "message", "status", "is_read"
                ])
            finance_qs.exclude(pk=finance_notification.pk).delete()

        invalidate_outward_cache()
        return Response({
            "status": "MANAGEMENT_REJECTED",
            "detail": "Physical drone Sale rejected by Management.",
            "reason": reason,
            "drone_instance_id": getattr(drone_instance, "pk", None),
            "sales": self.get_serializer(sales_rows, many=True).data,
        }, status=status.HTTP_200_OK)

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
        - Fully issued / In-Drone MR
        - Engineer selects DAMAGED / SCRAP serial numbers
        - Manager chooses Rebuild YES/NO during approval
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
                .exclude(
                    inventory_allocations__workflow__in=[
                        "RETURNABLE_DRONE_QC_V1",
                        "RETURNABLE_COMPONENT_QC_V1",
                    ],
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
            selected_drone_instance = None
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

                current_mr_status = (
                    str(
                        material_request.status
                        or ""
                    )
                    .strip()
                    .upper()
                )

                if (
                    current_mr_status
                    not in self.get_engineer_in_drone_statuses()
                ):
                    raise ValidationError(
                        {
                            "material_request": (
                                "Only a fully issued / In-Drone "
                                "Material Request can be used "
                                "for Engineer Scrap."
                            )
                        }
                    )

                raw_drone_instance = (
                    request.data.get("drone_instance_id")
                    or request.data.get("droneInstanceId")
                    or request.data.get("drone_instance_code")
                    or request.data.get("droneInstanceCode")
                )

                ensure_drone_instances(material_request, lock=True)
                refresh_drone_instance_statuses(material_request)

                if raw_drone_instance:
                    selected_drone_instance = self._drone_instance_from_reference(
                        material_request, raw_drone_instance, lock=True
                    )
                    if selected_drone_instance is None:
                        raise ValidationError({
                            "drone_instance_id": "Selected physical drone was not found."
                        })
                    if str(selected_drone_instance.status or "").upper() != "AVAILABLE":
                        raise ValidationError({
                            "drone_instance_id": (
                                f"{selected_drone_instance.instance_code} is not available "
                                f"for Scrap. Current state: "
                                f"{selected_drone_instance.get_status_display()}."
                            )
                        })

                    component_snapshot = []
                    for allocation in (
                        DroneComponentAllocation.objects.select_related("component")
                        .filter(drone_instance=selected_drone_instance, quantity__gt=0)
                        .order_by("component_id")
                    ):
                        serials = self.normalize_serials(allocation.serial_numbers)
                        component = allocation.component
                        code = str(getattr(component, "component_id", "") or "").strip()
                        name = str(getattr(component, "name", "") or "").strip()
                        label = " - ".join(v for v in (code, name) if v) or code or name or "Component"
                        component_snapshot.append({
                            "component": component.pk,
                            "component_code": code,
                            "component_name": name,
                            "label": label,
                            "issued_serials": serials,
                            "available_serials": serials,
                            "issued_quantity": int(allocation.quantity or 0),
                            "requested_quantity": int(allocation.quantity or 0),
                        })
                else:
                    # Legacy MR-level request. Keep the previous compatibility
                    # behavior, but new UI always sends one DroneInstance.
                    unavailable_reason = self.get_engineer_scrap_mr_unavailable_reason(
                        material_request
                    )
                    if unavailable_reason:
                        raise ValidationError({
                            "material_request": (
                                "This Material Request is not currently available for "
                                "Engineer Scrap. Select an AVAILABLE physical drone instance."
                            )
                        })
                    component_snapshot = self.get_engineer_scrap_component_snapshot(
                        material_request, lock=True
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

                # The Engineer no longer decides the disposition. Manager
                # records YES/NO during approval; Finance executes it later.
                reorder_choice = "PENDING_MANAGER"

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

                # -------------------------------------------------
                # FINAL Engineer Scrap selection rule
                # -------------------------------------------------
                # PARTIAL:
                #   SELECTED serials   = DAMAGED / SCRAP
                #   UNSELECTED serials = GOOD / REUSABLE
                #
                # Manager Reorder YES:
                #   GOOD serials       -> reused in NEW From-Scrap MR
                #   SCRAP serials      -> missing quantity is fulfilled
                #                         from In Store / Procurement
                #
                # Manager Reorder NO:
                #   GOOD serials       -> return to In Store
                #   SCRAP serials      -> remain Scrap
                #
                # TOTAL:
                #   all available serials -> SCRAP
                # -------------------------------------------------

                scrap_items = []
                good_items = []
                return_items = []
                reorder_items = []

                if scrap_mode == "TOTAL":
                    scrap_items = (
                        all_available_items
                    )
                else:
                    # The serials selected by Engineer are the actual
                    # damaged/Scrap serials.
                    scrap_items = (
                        validated_scrap_items
                    )

                    selected_scrap_by_component = {
                        str(
                            item["component"]
                        ): set(
                            item[
                                "serial_numbers"
                            ]
                        )
                        for item in scrap_items
                    }

                    # Everything not selected is GOOD / reusable.
                    for item in all_available_items:
                        selected_scrap_serials = (
                            selected_scrap_by_component
                            .get(
                                str(
                                    item[
                                        "component"
                                    ]
                                ),
                                set(),
                            )
                        )

                        good_serials = [
                            serial
                            for serial in item[
                                "serial_numbers"
                            ]
                            if serial
                            not in selected_scrap_serials
                        ]

                        if good_serials:
                            good_items.append(
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
                                        good_serials,
                                    "quantity":
                                        len(
                                            good_serials
                                        ),
                                }
                            )

                # Keep selected_items as the reusable list for backward
                # compatibility with Manager/Finance/Scrap audit screens.
                # New code can use good_items explicitly.
                selected_items = (
                    good_items
                )

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
                    "source_mr_request_type":
                        material_request.request_type,
                    "source_mr_customized_bom":
                        bool(material_request.customized_bom),
                    "drone_instance_id": (
                        selected_drone_instance.pk
                        if selected_drone_instance is not None
                        else None
                    ),
                    "drone_instance_code": (
                        selected_drone_instance.instance_code
                        if selected_drone_instance is not None
                        else ""
                    ),
                    "drone_instance_suffix": (
                        selected_drone_instance.suffix
                        if selected_drone_instance is not None
                        else ""
                    ),
                    # Backward-compatible reusable/good list.
                    "selected_items":
                        selected_items,

                    # Explicit reusable/good list for the new selection rule.
                    "good_items":
                        good_items,

                    # Engineer-selected damaged/Scrap items.
                    "scrap_items":
                        scrap_items,

                    # Populated only after Manager selects NO.
                    "return_items":
                        return_items,

                    # Populated only after Manager selects YES.
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
                    "manager_disposition_decision": "",
                    "manager_decided_by": "",
                    "manager_decided_at": "",
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

            if selected_drone_instance is not None:
                selected_drone_instance.status = "SCRAP_PENDING"
                selected_drone_instance.workflow_metadata = {
                    "workflow": "SCRAP",
                    "scrap_id": instance.pk,
                    "scrap_code": instance.code,
                    "scrap_mode": workflow_metadata.get("scrap_mode", ""),
                }
                selected_drone_instance.save(
                    update_fields=["status", "workflow_metadata", "updated_at"]
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

            # Backward-compatible idempotent endpoint. Finance approval now
            # executes the disposition; this call only confirms the already
            # processed result for older clients.
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

            # Finance is the final authority. Execute the Manager-selected
            # disposition here, inside the same transaction.
            instance.inventory_allocations = (
                self.process_engineer_scrap_disposition(
                    scrap_entry=instance,
                )
            )

            metadata = (
                instance.inventory_allocations
                if isinstance(instance.inventory_allocations, dict)
                else {}
            )
            workflow = str(metadata.get("workflow") or "").strip().upper()
            restore_choice = str(metadata.get("reorder_choice") or "NO").strip().upper()

            if (
                workflow in {
                    "RETURNABLE_COMPONENT_QC_V1",
                    "RETURNABLE_DRONE_QC_V1",
                }
                and restore_choice == "YES"
            ):
                # Manager chose Restore/Reorder YES and Finance has now
                # approved the Scrap disposition. Procurement is the next
                # stage and is allowed to create the replacement PO.
                metadata[
                    "procurement_restore_ready"
                ] = True
                metadata[
                    "procurement_restore_status"
                ] = "PENDING_PROCUREMENT"

                source_mr = str(
                    metadata.get(
                        "source_mr_number"
                    )
                    or ""
                ).strip()

                child_number = str(
                    metadata.get(
                        "replacement_mr_number"
                    )
                    or ""
                ).strip()

                failed_items = (
                    metadata.get(
                        "failed_items"
                    )
                    or metadata.get(
                        "scrap_items"
                    )
                    or []
                )

                failed_summary = ", ".join(
                    (
                        f"{item.get('component_name') or item.get('label') or item.get('component_code') or 'Component'}"
                        f" - {int(item.get('quantity') or len(item.get('serial_numbers') or []) or 0)}"
                    )
                    for item in failed_items
                    if isinstance(item, dict)
                )

                Notification.objects.update_or_create(
                    category="QC_FAILED",
                    receiver="PROCUREMENT",
                    reference_id=(
                        f"OUTWARD:{instance.pk}"
                    ),
                    defaults={
                        "requested_by":
                            instance.requested_by
                            or finance_name,
                        "title": (
                            "Returnable Reorder Required - "
                            f"{child_number or source_mr or instance.code}"
                        ),
                        "message": (
                            "Manager and Finance approved the Returnable "
                            "QC Failed reorder/restore. "
                            + (
                                f"Child MR: {child_number}. "
                                if child_number
                                else ""
                            )
                            + (
                                f"Failed quantity: {failed_summary}. "
                                if failed_summary
                                else ""
                            )
                            + "Procurement can now approve Restore and "
                            "raise the replacement PO."
                        ),
                        "status":
                            "PENDING_PROCUREMENT",
                        "is_read":
                            False,
                    },
                )

                # Keep returnable audit state approved while the separate
                # restore/replacement status moves through Procurement/PO.
                self.sync_returnable_usage_status(
                    metadata,
                    "APPROVED",
                )
            else:
                # Final Scrap / retirement / return-to-store completes here.
                self.sync_returnable_usage_status(
                    metadata,
                    "COMPLETED",
                )

            instance.inventory_allocations = metadata

            instance.moved_to_inventory = True
            instance.moved_at = timezone.now()
            instance.stock_restored = bool(
                isinstance(instance.inventory_allocations, dict)
                and instance.inventory_allocations.get(
                    "returned_inventory_ids"
                )
            )

            instance.save(
                update_fields=[
                    "approval_status",
                    "status",
                    "rejection_reason",
                    "rejected_by",
                    "inventory_allocations",
                    "moved_to_inventory",
                    "moved_at",
                    "stock_restored",
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

            metadata = (
                instance.inventory_allocations
                if isinstance(instance.inventory_allocations, dict)
                else {}
            )
            self.sync_returnable_usage_status(
                metadata,
                "REJECTED",
                reason=f"Finance rejected: {reason}",
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
        url_path="request-returnable-restore",
    )
    @transaction.atomic
    def request_returnable_restore(
        self,
        request,
        pk=None,
    ):
        """
        Inventory starts Restore ONLY for a component that:
            1. belonged to a Returnable MR,
            2. was issued to the Engineer,
            3. was returned by the Engineer,
            4. failed Inventory Return QC.

        No Manager approval is used.

        ACTION_REQUIRED
            -> PENDING_PROCUREMENT
            -> Procurement approves and automatically creates replacement PO(s)
            -> PENDING_FINANCE
            -> Finance approval
            -> Procurement orders
            -> Delivery / Inward / QC
            -> QC passed quantity goes to Central In Store.
        """
        user = getattr(
            request,
            "user",
            None,
        )

        if (
            not user
            or not getattr(
                user,
                "is_authenticated",
                False,
            )
        ):
            raise PermissionDenied(
                "Authentication is required."
            )

        active_role = self.get_active_role(
            request
        )

        if (
            active_role
            not in {
                "inventory",
                "admin",
            }
            and not getattr(
                user,
                "is_superuser",
                False,
            )
        ):
            raise PermissionDenied(
                "Only Inventory can request Restore for a returned QC-failed component."
            )

        instance = (
            self.get_queryset()
            .select_for_update()
            .get(pk=pk)
        )

        metadata = (
            instance.inventory_allocations
            if isinstance(
                instance.inventory_allocations,
                dict,
            )
            else {}
        )

        workflow = str(
            metadata.get(
                "workflow"
            )
            or ""
        ).strip().upper()

        if (
            workflow
            != "RETURNABLE_COMPONENT_QC_V1"
        ):
            raise ValidationError(
                {
                    "detail": (
                        "Restore is available only for a Returnable "
                        "component after Engineer return and failed Return QC."
                    )
                }
            )

        failed_items = (
            metadata.get(
                "scrap_items",
                [],
            )
            or metadata.get(
                "failed_items",
                [],
            )
            or []
        )

        if not failed_items:
            raise ValidationError(
                {
                    "detail":
                        "No failed Returnable component quantity is available for Restore."
                }
            )

        current_restore_status = str(
            metadata.get(
                "procurement_restore_status"
            )
            or "ACTION_REQUIRED"
        ).strip().upper()

        if metadata.get(
            "restore_po_id"
        ) or metadata.get(
            "restore_po_ids"
        ):
            return Response(
                {
                    "detail":
                        "Restore Purchase Order has already been created.",
                    "status":
                        current_restore_status,
                    "restore_po_id":
                        metadata.get(
                            "restore_po_id"
                        ),
                    "restore_po_ids":
                        metadata.get(
                            "restore_po_ids"
                        )
                        or [],
                },
                status=status.HTTP_200_OK,
            )

        if current_restore_status in {
            "PENDING_PROCUREMENT",
            "PROCUREMENT_APPROVED",
            "PENDING_FINANCE",
            "FINANCE_APPROVED",
            "PO_RAISED",
            "ORDERED",
            "COMPLETED_IN_STORE",
        }:
            return Response(
                {
                    "detail":
                        "Restore has already been requested.",
                    "status":
                        current_restore_status,
                },
                status=status.HTTP_200_OK,
            )

        actor = self.get_actor_name(
            user
        )

        metadata[
            "procurement_restore_ready"
        ] = True

        metadata[
            "procurement_restore_status"
        ] = "PENDING_PROCUREMENT"

        metadata[
            "restore_requested_by"
        ] = actor

        metadata[
            "restore_requested_at"
        ] = timezone.now().isoformat()

        instance.inventory_allocations = (
            metadata
        )

        instance.approval_status = (
            "RESTORE_PENDING_PROCUREMENT"
        )

        instance.status = (
            "RESTORE_PENDING_PROCUREMENT"
        )

        instance.save(
            update_fields=[
                "inventory_allocations",
                "approval_status",
                "status",
                "updated_at",
            ]
        )

        source_mr = str(
            metadata.get(
                "source_mr_number"
            )
            or ""
        ).strip()

        failed_summary = ", ".join(
            (
                f"{item.get('component_name') or item.get('label') or item.get('component_code') or 'Component'}"
                f" - {int(item.get('quantity') or len(item.get('serial_numbers') or []) or 0)}"
            )
            for item in failed_items
            if isinstance(
                item,
                dict,
            )
        )

        Notification.objects.update_or_create(
            category="QC_FAILED",
            receiver="PROCUREMENT",
            reference_id=(
                f"OUTWARD:{instance.pk}"
            ),
            defaults={
                "requested_by":
                    actor,
                "title": (
                    "Returnable Restore Approval - "
                    f"{source_mr or instance.code}"
                ),
                "message": (
                    "Returned component failed Inventory QC. "
                    "Approve Restore to automatically raise replacement "
                    "PO only for the failed quantity. "
                    f"{failed_summary}"
                ),
                "status":
                    "PENDING_PROCUREMENT",
                "is_read":
                    False,
            },
        )

        # Linked ComponentUsage rows remain permanent audit rows.
        from componentusage.models import ComponentUsage

        usage_ids = (
            metadata.get(
                "returnable_usage_ids"
            )
            or []
        )

        for usage in (
            ComponentUsage.objects
            .select_for_update()
            .filter(
                pk__in=usage_ids
            )
        ):
            details = (
                usage.inventory_issue_details
                if isinstance(
                    usage.inventory_issue_details,
                    list,
                )
                else []
            )

            if not details:
                details = [{}]

            details[0][
                "restore_status"
            ] = "PENDING_PROCUREMENT"

            details[0][
                "restore_outward_id"
            ] = instance.pk

            usage.inventory_issue_details = (
                details
            )

            usage.save(
                update_fields=[
                    "inventory_issue_details"
                ]
            )

        return Response(
            {
                "detail":
                    "Restore sent to Procurement for approval.",
                "status":
                    "PENDING_PROCUREMENT",
                "outward_id":
                    instance.pk,
            },
            status=status.HTTP_200_OK,
        )


    @action(
        detail=True,
        methods=["post"],
        url_path="raise-returnable-restore-po",
    )
    @transaction.atomic
    def raise_returnable_restore_po(
        self,
        request,
        pk=None,
    ):
        """
        Procurement approval for a Returnable Restore request.

        Procurement DOES NOT manually re-enter vendor/component/quantity.

        The backend resolves the original PO line for every failed component
        and creates replacement PO(s) with:
            - same original vendor
            - same failed component
            - failed quantity only
            - same source Returnable MR
            - REPLACEMENT_PENDING_FINANCE

        Finance is the next approval stage.
        """
        user = getattr(
            request,
            "user",
            None,
        )

        if (
            not user
            or not getattr(
                user,
                "is_authenticated",
                False,
            )
        ):
            raise PermissionDenied(
                "Authentication is required."
            )

        active_role = self.get_active_role(
            request
        )

        if (
            active_role
            not in {
                "procurement",
                "admin",
            }
            and not getattr(
                user,
                "is_superuser",
                False,
            )
        ):
            raise PermissionDenied(
                "Only Procurement can approve a Returnable Restore request."
            )

        instance = (
            self.get_queryset()
            .select_for_update()
            .get(pk=pk)
        )

        metadata = (
            instance.inventory_allocations
            if isinstance(
                instance.inventory_allocations,
                dict,
            )
            else {}
        )

        restore_workflow = str(
            metadata.get("workflow") or ""
        ).strip().upper()

        if restore_workflow not in {
            "RETURNABLE_COMPONENT_QC_V1",
            "RETURNABLE_DRONE_QC_V1",
        }:
            raise ValidationError(
                {
                    "detail": (
                        "Restore/Reorder PO is available only for failed "
                        "Returnable component or drone QC records."
                    )
                }
            )

        restore_status = str(
            metadata.get(
                "procurement_restore_status"
            )
            or ""
        ).strip().upper()

        if metadata.get(
            "restore_po_ids"
        ) or metadata.get(
            "restore_po_id"
        ):
            return Response(
                {
                    "detail":
                        "Restore Purchase Order already exists.",
                    "purchase_order_ids":
                        metadata.get(
                            "restore_po_ids"
                        )
                        or [
                            metadata.get(
                                "restore_po_id"
                            )
                        ],
                    "po_numbers":
                        metadata.get(
                            "restore_po_numbers"
                        )
                        or [
                            metadata.get(
                                "restore_po_number"
                            )
                        ],
                    "status":
                        restore_status,
                },
                status=status.HTTP_200_OK,
            )

        if (
            restore_status
            != "PENDING_PROCUREMENT"
        ):
            raise ValidationError(
                {
                    "detail": (
                        "This Restore request is not pending Procurement approval. "
                        f"Current state: {restore_status or 'UNKNOWN'}."
                    )
                }
            )

        original_source_mr_number = str(
            metadata.get("source_mr_number") or ""
        ).strip()

        source_mr_number = str(
            metadata.get("replacement_mr_number")
            or original_source_mr_number
            or ""
        ).strip()

        if not original_source_mr_number:
            raise ValidationError(
                {
                    "detail":
                        "Source Returnable Material Request is missing."
                }
            )

        failed_items = (
            metadata.get(
                "scrap_items",
                [],
            )
            or metadata.get(
                "failed_items",
                [],
            )
            or []
        )

        if not failed_items:
            raise ValidationError(
                {
                    "detail":
                        "No failed component quantities are available for Restore."
                }
            )

        # Resolve the exact original PO line for every failed component.
        grouped_by_root_po = {}

        for failed in failed_items:
            if not isinstance(
                failed,
                dict,
            ):
                continue

            component_id = (
                failed.get(
                    "component_id"
                )
                or failed.get(
                    "component"
                )
            )

            quantity = max(
                int(
                    failed.get(
                        "quantity"
                    )
                    or len(
                        failed.get(
                            "serial_numbers"
                        )
                        or []
                    )
                    or 0
                ),
                0,
            )

            if (
                not component_id
                or quantity <= 0
            ):
                continue

            source_item = (
                PurchaseOrderItem.objects
                .select_for_update()
                .select_related(
                    "purchase_order"
                )
                .filter(
                    purchase_order__source_mr_number=
                        original_source_mr_number,
                    component_id=
                        component_id,
                )
                .exclude(
                    purchase_order__status__in=[
                        "DRAFT",
                        "REJECTED",
                        "FINANCE_REJECTED",
                        "REPLACEMENT_MANAGER_REJECTED",
                        "REPLACEMENT_FINANCE_REJECTED",
                    ]
                )
                .order_by(
                    "-purchase_order_id",
                    "-id",
                )
                .first()
            )

            if source_item is None:
                # Returned components may originally have been fulfilled from
                # Central In Store rather than purchased specifically for this
                # Returnable MR.
                #
                # Recover source lineage from the exact returned serial.
                # Inventory rows retain issued_serial_numbers plus the
                # original purchase_order text.
                failed_serials = (
                    self.normalize_serials(
                        failed.get(
                            "serial_numbers"
                        )
                        or []
                    )
                )

                source_inventory_row = None

                if failed_serials:
                    inventory_candidates = (
                        Inventory.objects
                        .select_for_update()
                        .filter(
                            component_id=
                                component_id,
                        )
                        .order_by(
                            "-id"
                        )
                    )

                    failed_serial_set = set(
                        failed_serials
                    )

                    for inventory_row in (
                        inventory_candidates
                    ):
                        issued_serials = (
                            self.normalize_serials(
                                getattr(
                                    inventory_row,
                                    "issued_serial_numbers",
                                    [],
                                )
                                or []
                            )
                        )

                        current_serials = (
                            self.normalize_serials(
                                getattr(
                                    inventory_row,
                                    "serial_numbers",
                                    [],
                                )
                                or []
                            )
                        )

                        if failed_serial_set.intersection(
                            set(
                                issued_serials
                                + current_serials
                            )
                        ):
                            source_inventory_row = (
                                inventory_row
                            )
                            break

                if (
                    source_inventory_row
                    is not None
                ):
                    inventory_po_number = str(
                        getattr(
                            source_inventory_row,
                            "purchase_order",
                            "",
                        )
                        or ""
                    ).strip()

                    if inventory_po_number:
                        source_item = (
                            PurchaseOrderItem.objects
                            .select_for_update()
                            .select_related(
                                "purchase_order"
                            )
                            .filter(
                                purchase_order__po_number=
                                    inventory_po_number,
                                component_id=
                                    component_id,
                            )
                            .order_by(
                                "-purchase_order_id",
                                "-id",
                            )
                            .first()
                        )

            if source_item is None:
                raise ValidationError(
                    {
                        "detail": (
                            "The original vendor/PO could not be automatically "
                            "resolved for failed component "
                            f"{failed.get('component_name') or failed.get('label') or component_id}. "
                            "The exact returned serial must remain traceable to "
                            "its source Inventory / Purchase Order before Restore "
                            "can automatically create a same-vendor PO."
                        )
                    }
                )

            immediate_po = (
                source_item.purchase_order
            )

            root_po = (
                immediate_po.replacement_for
                if (
                    str(
                        getattr(
                            immediate_po,
                            "order_type",
                            "STANDARD",
                        )
                        or "STANDARD"
                    )
                    .strip()
                    .upper()
                    == "REPLACEMENT"
                    and immediate_po
                    .replacement_for_id
                )
                else immediate_po
            )

            group = (
                grouped_by_root_po
                .setdefault(
                    root_po.pk,
                    {
                        "root_po":
                            root_po,
                        "immediate_po":
                            immediate_po,
                        "items":
                            [],
                    },
                )
            )

            group["items"].append(
                {
                    "component_id":
                        component_id,
                    "quantity":
                        quantity,
                    "unit_price":
                        source_item.unit_price,
                    "gst_percentage":
                        source_item
                        .gst_percentage,
                    "expected_delivery_date":
                        (
                            source_item
                            .expected_delivery_date
                            or immediate_po
                            .expected_delivery_date
                        ),
                }
            )

        if not grouped_by_root_po:
            raise ValidationError(
                {
                    "detail":
                        "No valid failed components were available for Restore."
                }
            )

        actor = self.get_actor_name(
            user
        )

        created_pos = []

        for group in (
            grouped_by_root_po.values()
        ):
            root_po = group[
                "root_po"
            ]

            immediate_po = group[
                "immediate_po"
            ]

            last_round = (
                PurchaseOrder.objects
                .filter(
                    replacement_for=
                        root_po,
                    order_type=
                        "REPLACEMENT",
                )
                .aggregate(
                    max_round=Max(
                        "replacement_round"
                    )
                )
                .get(
                    "max_round"
                )
                or 0
            )

            replacement_round = (
                int(last_round)
                + 1
            )

            po_number = (
                f"{root_po.po_number}"
                f"-R{replacement_round}"
            )

            while (
                PurchaseOrder.objects
                .filter(
                    po_number=po_number
                )
                .exists()
            ):
                replacement_round += 1

                po_number = (
                    f"{root_po.po_number}"
                    f"-R{replacement_round}"
                )

            expected_dates = [
                item.get(
                    "expected_delivery_date"
                )
                for item in group[
                    "items"
                ]
                if item.get(
                    "expected_delivery_date"
                )
            ]

            po_expected = (
                max(expected_dates)
                if expected_dates
                else None
            )

            po = (
                PurchaseOrder.objects
                .create(
                    po_number=
                        po_number,
                    vendor_name=
                        immediate_po
                        .vendor_name,
                    gstin=
                        immediate_po
                        .gstin,
                    location=
                        immediate_po
                        .location,
                    ordered_date=
                        None,
                    expected_delivery_date=
                        po_expected,
                    remarks=(
                        "RETURNABLE_RESTORE "
                        f"RETURNABLE_RESTORE_OUTWARD:{instance.pk}; "
                        f"Source Returnable MR: {original_source_mr_number}; "
                        f"Reorder MR: {source_mr_number}; "
                        f"Failed-QC Outward: {instance.code}."
                    ),
                    finance_remarks=
                        None,
                    status=
                        "REPLACEMENT_PENDING_FINANCE",
                    approval_status=
                        "REPLACEMENT_PENDING_FINANCE",
                    source_mr_number=
                        source_mr_number,
                    order_type=
                        "REPLACEMENT",
                    replacement_for=
                        root_po,
                    replacement_round=
                        replacement_round,
                    replacement_source_inward_id=
                        None,
                )
            )

            for item in group[
                "items"
            ]:
                PurchaseOrderItem.objects.create(
                    purchase_order=
                        po,
                    component_id=
                        item[
                            "component_id"
                        ],
                    quantity=
                        item[
                            "quantity"
                        ],
                    received_quantity=
                        0,
                    unit_price=
                        item[
                            "unit_price"
                        ],
                    gst_percentage=
                        item[
                            "gst_percentage"
                        ],
                    expected_delivery_date=
                        item[
                            "expected_delivery_date"
                        ],
                )

            PurchaseOrderApproval.objects.create(
                purchase_order=po,
                action=
                    "REPLACEMENT_REQUESTED",
                requested_by=
                    actor,
                approved_by=
                    actor,
            )

            Notification.objects.update_or_create(
                category="PO",
                receiver="FINANCE",
                reference_id=str(
                    po.pk
                ),
                defaults={
                    "requested_by":
                        actor,
                    "title": (
                        "Returnable Restore Finance Approval - "
                        f"{po.po_number}"
                    ),
                    "message": (
                        "Procurement approved the Returnable QC Restore. "
                        f"Replacement PO {po.po_number} is waiting for Finance approval."
                    ),
                    "status":
                        "REPLACEMENT_PENDING_FINANCE",
                    "is_read":
                        False,
                },
            )

            created_pos.append(
                po
            )

        restore_po_ids = [
            po.pk
            for po in created_pos
        ]

        restore_po_numbers = [
            po.po_number
            for po in created_pos
        ]

        metadata[
            "procurement_restore_ready"
        ] = False

        metadata[
            "procurement_restore_status"
        ] = "PENDING_FINANCE"

        metadata[
            "restore_po_ids"
        ] = restore_po_ids

        metadata[
            "restore_po_numbers"
        ] = restore_po_numbers

        # Backward-compatible singular values when only one PO exists.
        metadata[
            "restore_po_id"
        ] = (
            restore_po_ids[0]
            if len(
                restore_po_ids
            )
            == 1
            else None
        )

        metadata[
            "restore_po_number"
        ] = (
            restore_po_numbers[0]
            if len(
                restore_po_numbers
            )
            == 1
            else ""
        )

        metadata[
            "restore_po_raised_by"
        ] = actor

        metadata[
            "restore_po_raised_at"
        ] = timezone.now().isoformat()

        replacement_mr_id = metadata.get("replacement_mr_id")
        if replacement_mr_id:
            MaterialRequest.objects.filter(pk=replacement_mr_id).update(
                status="PO_RAISED",
                approval_status="MANAGER_APPROVED",
                po_raised=True,
            )

        instance.inventory_allocations = (
            metadata
        )

        instance.approval_status = (
            "RESTORE_PENDING_FINANCE"
        )

        instance.status = (
            "RESTORE_PENDING_FINANCE"
        )

        instance.save(
            update_fields=[
                "inventory_allocations",
                "approval_status",
                "status",
                "updated_at",
            ]
        )

        Notification.objects.filter(
            category="QC_FAILED",
            receiver="PROCUREMENT",
            reference_id=(
                f"OUTWARD:{instance.pk}"
            ),
        ).update(
            status=
                "PROCUREMENT_APPROVED",
            is_read=True,
        )

        # Keep linked Returnable rows auditable while restore PO(s)
        # move through Finance -> Ordered -> Inward -> QC -> In Store.
        from componentusage.models import ComponentUsage

        usage_ids = (
            metadata.get(
                "returnable_usage_ids"
            )
            or []
        )

        for usage in (
            ComponentUsage.objects
            .select_for_update()
            .filter(
                pk__in=usage_ids
            )
        ):
            details = (
                usage.inventory_issue_details
                if isinstance(
                    usage.inventory_issue_details,
                    list,
                )
                else []
            )

            if not details:
                details = [{}]

            details[0].update(
                {
                    "restore_po_ids":
                        restore_po_ids,
                    "restore_po_numbers":
                        restore_po_numbers,
                    "restore_status":
                        "PENDING_FINANCE",
                    "restore_outward_id":
                        instance.pk,
                }
            )

            usage.inventory_issue_details = (
                details
            )

            usage.save(
                update_fields=[
                    "inventory_issue_details"
                ]
            )

        return Response(
            {
                "detail": (
                    "Procurement approved Restore. "
                    "Replacement PO is now pending Finance approval."
                ),
                "purchase_order_ids":
                    restore_po_ids,
                "po_numbers":
                    restore_po_numbers,
                "status":
                    "PENDING_FINANCE",
            },
            status=status.HTTP_201_CREATED,
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
        """Manager disposition.

        Returnable QC:
            YES -> Procurement -> replacement PO -> Finance
            NO  -> Finance -> final QC-failed Scrap

        Existing Engineer Scrap keeps its original Manager -> Finance route.
        """
        user = self.require_manager(request)
        reorder_choice = str(
            request.data.get(
                "reorder_choice",
                request.data.get("reorderChoice", ""),
            )
            or ""
        ).strip().upper()

        with transaction.atomic():
            instance = (
                self.get_queryset()
                .select_for_update()
                .get(pk=pk)
            )

            if str(instance.outward_type or "").strip().upper() != "SCRAP":
                raise ValidationError({
                    "detail": "Only Scrap/QC-failed disposition records require Manager approval."
                })

            current = str(instance.approval_status or "").strip().upper()
            if current != "PENDING_MANAGER":
                raise ValidationError({
                    "detail": (
                        "This request is no longer pending Manager approval. "
                        f"Current state: {current or 'UNKNOWN'}."
                    )
                })

            manager_name = self.get_actor_name(user)
            metadata = (
                instance.inventory_allocations
                if isinstance(instance.inventory_allocations, dict)
                else {}
            )
            workflow = str(metadata.get("workflow") or "").strip().upper()
            supported = {
                "ENGINEER_MR_SCRAP_DISPOSITION_V1",
                "RETURNABLE_DRONE_QC_V1",
                "RETURNABLE_COMPONENT_QC_V1",
            }

            if workflow in supported and reorder_choice not in {"YES", "NO"}:
                raise ValidationError({
                    "reorder_choice": "Choose YES to reorder/replace or NO for final Scrap."
                })

            selected_items = (
                metadata.get("good_items", [])
                or metadata.get("selected_items", [])
                or []
            )
            failed_items = (
                metadata.get("failed_items", [])
                or metadata.get("scrap_items", [])
                or []
            )

            metadata["reorder_choice"] = reorder_choice
            metadata["manager_disposition_decision"] = reorder_choice
            metadata["manager_decided_by"] = manager_name
            metadata["manager_decided_at"] = timezone.now().isoformat()
            metadata["return_items"] = selected_items if reorder_choice == "NO" else []
            metadata["reorder_items"] = failed_items if reorder_choice == "YES" else []
            metadata["return_quantity"] = sum(
                int(item.get("quantity", 0) or 0)
                for item in metadata["return_items"]
                if isinstance(item, dict)
            )
            metadata["reorder_quantity"] = sum(
                int(item.get("quantity", 0) or 0)
                for item in metadata["reorder_items"]
                if isinstance(item, dict)
            )

            is_returnable_qc = workflow in {
                "RETURNABLE_DRONE_QC_V1",
                "RETURNABLE_COMPONENT_QC_V1",
            }

            # ---------------------------------------------------------
            # MANAGER -> FINANCE -> PROCUREMENT
            # ---------------------------------------------------------
            #
            # Returnable Reorder YES used to bypass Finance here:
            #
            #   Manager -> Procurement -> Replacement PO -> Finance
            #
            # and explicitly deleted the Finance SCRAP notification.
            #
            # Scrap disposition is a two-stage approval flow. Therefore BOTH
            # YES and NO decisions must first reach Finance:
            #
            #   Manager
            #      -> Finance Scrap approval
            #      -> Procurement (only when Reorder/Restore = YES)
            #      -> Replacement PO
            #      -> Finance PO approval
            #
            # For returned-drone QC we still create the visible _PR/_FR child
            # MR at Manager YES so the tracking row remains available, but
            # Procurement is not notified until Finance approves this Scrap.
            if is_returnable_qc and reorder_choice == "YES":
                source_mr = None

                if instance.material_request_id:
                    source_mr = (
                        MaterialRequest.objects
                        .select_for_update()
                        .filter(
                            pk=instance.material_request_id
                        )
                        .first()
                    )

                if (
                    workflow == "RETURNABLE_DRONE_QC_V1"
                    and source_mr is not None
                    and not metadata.get(
                        "replacement_mr_id"
                    )
                ):
                    child_mr = (
                        self.create_returnable_qc_reorder_mr(
                            scrap_entry=instance,
                            source_mr=source_mr,
                            failed_items=failed_items,
                            good_items=(
                                metadata.get(
                                    "good_items"
                                )
                                or []
                            ),
                        )
                    )

                    metadata[
                        "replacement_mr_id"
                    ] = child_mr.pk
                    metadata[
                        "replacement_mr_number"
                    ] = (
                        child_mr.material_request_id
                    )
                    metadata[
                        "returnable_reorder_type"
                    ] = (
                        "PR"
                        if child_mr
                        .material_request_id
                        .endswith("_PR")
                        else "FR"
                    )

                # Finance must approve before Procurement can act.
                metadata[
                    "procurement_restore_ready"
                ] = False
                metadata[
                    "procurement_restore_status"
                ] = "AWAITING_FINANCE"

            instance.inventory_allocations = metadata
            instance.approval_status = "PENDING_FINANCE"
            instance.status = "PENDING_FINANCE"
            instance.rejection_reason = None
            instance.rejected_by = None

            instance.save(
                update_fields=[
                    "approval_status",
                    "status",
                    "rejection_reason",
                    "rejected_by",
                    "inventory_allocations",
                    "updated_at",
                ]
            )

            if (
                workflow
                == "RETURNABLE_COMPONENT_QC_V1"
            ):
                disposition_label = (
                    "RESTORE / REPLACE"
                    if reorder_choice == "YES"
                    else "FINAL SCRAP"
                )
            elif (
                workflow
                == "RETURNABLE_DRONE_QC_V1"
            ):
                disposition_label = (
                    "REBUILD DRONE"
                    if reorder_choice == "YES"
                    else "FINAL SCRAP / RETIRE DRONE"
                )
            else:
                disposition_label = (
                    "REBUILD MR"
                    if reorder_choice == "YES"
                    else "RETURN TO STORE"
                )

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
                    f"Approved by {manager_name}; "
                    f"disposition {disposition_label}; "
                    "pending Finance approval."
                ),
            )

            # Remove a stale Procurement notification created by the old
            # Manager-direct-to-Procurement route. Procurement must wait for
            # Finance Scrap approval.
            if (
                is_returnable_qc
                and reorder_choice == "YES"
            ):
                Notification.objects.filter(
                    category="QC_FAILED",
                    receiver="PROCUREMENT",
                    reference_id=(
                        f"OUTWARD:{instance.pk}"
                    ),
                ).delete()

            self.sync_returnable_usage_status(
                metadata,
                "PENDING_FINANCE",
            )

            # This is the notification the Finance -> Scrap tab consumes.
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
            self.get_serializer(instance).data,
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

            # Manager rejection is final. Returnable usage stops here.
            metadata = (
                instance.inventory_allocations
                if isinstance(instance.inventory_allocations, dict)
                else {}
            )
            self.sync_returnable_usage_status(
                metadata,
                "REJECTED",
                reason=f"Manager rejected: {reason}",
            )

            # Remove any impossible stale Finance-stage notification.
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
